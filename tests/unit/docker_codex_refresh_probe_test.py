import base64
import copy
import json
import os
import runpy
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.case import Case

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/docker_codex_a_probe.py"


def auth(exp=2000000100, marker="before", refreshed="2033-05-18T03:00:00Z"):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": "private-id",
            "access_token": f"header.{payload}.{marker}",
            "refresh_token": "private-refresh",
            "account_id": "private-account",
        },
        "last_refresh": refreshed,
    }


class FakeStream:
    def __init__(self, number):
        self.number = number
        self.closed = False
        self.writes = []
        self.on_write = None

    def fileno(self):
        return self.number

    def write(self, data):
        self.writes.append(data)
        if self.on_write:
            self.on_write(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self):
        self.stdin, self.stdout, self.stderr = [FakeStream(number) for number in (10, 11, 12)]
        self.returncode = None
        self.pid = 123456789

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.returncode = 0
        return self.returncode


class FakeSelector:
    def __init__(self):
        self.entries = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def register(self, stream, _events, kind):
        self.entries[stream] = SimpleNamespace(fileobj=stream, data=kind)

    def unregister(self, stream):
        del self.entries[stream]

    def get_map(self):
        return self.entries

    def select(self, timeout):
        return [(next(iter(self.entries.values())), None)]


class RefreshProbeTest(Case):
    def setUp(self):
        super().setUp()
        self.module = runpy.run_path(str(SCRIPT))
        self.observed = datetime.fromtimestamp(2000000000, UTC)

    def summary(self, before, after):
        return self.module["summarize_refresh"](before, after, self.observed)

    def fake_root(self):
        root = Path(self.home) / "live"
        root.mkdir(mode=0o700)
        for name in ("home", "broker"):
            (root / name).mkdir(mode=0o700)
        (root / "codex").write_bytes(b"fake-native-data-never-executed")
        (root / "codex").chmod(0o700)
        (root / "broker/auth.json").write_text(json.dumps(auth()))
        (root / "broker/auth.json").chmod(0o600)
        return root

    @contextmanager
    def fake_host(self, root, *, version=b"codex-cli 0.163.0\n", after=None, rpc_error=False):
        blocks = {}
        spawned = []
        opened_paths = []
        real_open = os.open

        def opened(path, flags, mode=0o777, *, dir_fd=None):
            opened_paths.append(str(path))
            if path == "task647-refresh.lock":
                self.assertEqual(os.fstat(dir_fd).st_ino, (root / "broker").stat().st_ino)
                return real_open(root / "broker" / path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def spawn(argv, **kwargs):
            process = FakeProcess()
            spawned.append((argv, kwargs, process))
            blocks[11] = [version, b""] if argv[-1] == "--version" else []
            blocks[12] = [b"fake-secret-stderr", b""]

            def write(data):
                method = json.loads(data)["method"]
                if method == "initialize":
                    blocks[11].append(b'{"id":1,"result":{"secret":"fake-secret"}}\n')
                elif method == "account/read":
                    if after is not None:
                        (root / "broker/auth.json").write_text(json.dumps(after))
                    response = (
                        {"id": 2, "error": {"message": "fake-secret"}}
                        if rpc_error
                        else {
                            "id": 2,
                            "result": {
                                "account": {"type": "chatgpt", "email": "fake-secret"},
                                "requiresOpenaiAuth": True,
                            },
                        }
                    )
                    blocks[11].append(json.dumps(response).encode() + b"\n")

            process.stdin.on_write = write
            return process

        def read(number, _size):
            return blocks[number].pop(0) if blocks[number] else b""

        with (
            patch("subprocess.Popen", side_effect=spawn),
            patch("selectors.DefaultSelector", FakeSelector),
            patch("os.set_blocking"),
            patch("os.read", side_effect=read),
            patch("os.killpg"),
            patch("time.sleep"),
            patch("os.open", side_effect=opened),
        ):
            yield spawned, opened_paths

    @contextmanager
    def temp_lock_open(self, directory):
        opened = os.open

        def fake_open(path, flags, mode=0o777, *, dir_fd=None):
            if path == "task647-refresh.lock":
                self.assertEqual(os.fstat(dir_fd).st_ino, directory.stat().st_ino)
                return opened(directory / path, flags, mode)
            return opened(path, flags, mode, dir_fd=dir_fd)

        with patch("os.open", side_effect=fake_open):
            yield

    def run_transport(self, chunks, *, exchange=True, timeout=False, stderr=None):
        process = FakeProcess()
        blocks = {11: list(chunks), 12: stderr or [b"fake-secret-stderr", b""]}

        def read(number, _size):
            return blocks[number].pop(0) if blocks[number] else b""

        with (
            patch("subprocess.Popen", return_value=process) as spawn,
            patch("selectors.DefaultSelector", FakeSelector),
            patch("os.set_blocking"),
            patch("os.read", side_effect=read),
            patch("os.killpg") as killed,
            patch("time.sleep"),
            patch("time.monotonic", side_effect=[0, 121] if timeout else lambda: 0),
        ):
            state = self.module["RefreshExchange"]() if exchange else None
            result, output = self.module["run_native"](["fake-native"], {"HOME": self.home}, state)
        self.assertTrue(
            all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
        )
        self.assertNotIn("secret", json.dumps(result))
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(spawn.call_args.kwargs["cwd"], self.home)
        return result, output, process, killed

    def test_rotation_observed_without_refresh_token_rotation(self):
        result = self.summary(auth(), auth(2000000200, "after", "2033-05-18T04:01:00+01:00"))
        self.assertEqual(result["status"], "rotation_observed")
        self.assertTrue(result["access_changed"])
        self.assertFalse(result["refresh_changed"])
        text = json.dumps(result)
        for secret in ("private", "header.", "2033-05-18T04:01:00+01:00"):
            self.assertNotIn(secret, text)

    def test_rpc_success_without_rotation_is_not_pass(self):
        self.assertEqual(self.summary(auth(), auth())["status"], "incomplete")

    def test_identity_changes_and_invalid_credentials_rejected(self):
        for key, value in (("account_id", "other"), ("refresh_token", ""), ("id_token", None)):
            after = auth(2000000200, "after", "2033-05-18T03:01:00Z")
            after["tokens"][key] = value
            self.assertEqual(self.summary(auth(), after)["status"], "incomplete")
        after = auth()
        after["auth_mode"] = "chatgptAuthTokens"
        self.assertEqual(self.summary(auth(), after)["status"], "incomplete")

    def test_invalid_or_nonadvancing_dates_not_exported(self):
        for stamp in ("private-date", None, "2033-05-18T03:00:00", "2033-05-18T03:00:00Z"):
            after = auth(2000000200, "after", stamp)
            result = self.summary(auth(), after)
            self.assertEqual(result["status"], "incomplete")
            self.assertNotIn("private-date", json.dumps(result))

    def test_exchange_orders_handshake_and_suppresses_notifications(self):
        exchange = self.module["RefreshExchange"]()
        first = json.loads(exchange.start())
        self.assertEqual(first["method"], "initialize")
        self.assertTrue(first["params"]["capabilities"]["explicitGatewayOauth"])
        self.assertEqual(
            exchange.accept({"method": "warning", "params": {"message": "secret"}}), []
        )
        sent = [
            json.loads(line) for line in exchange.accept({"id": 1, "result": {"secret": "secret"}})
        ]
        self.assertEqual([row["method"] for row in sent], ["initialized", "account/read"])
        self.assertEqual(sent[1]["params"], {"refreshToken": True})
        self.assertEqual(
            exchange.accept(
                {
                    "id": 2,
                    "result": {
                        "account": {"type": "chatgpt", "email": "secret"},
                        "requiresOpenaiAuth": True,
                    },
                }
            ),
            [],
        )
        self.assertTrue(exchange.complete)
        self.assertNotIn("secret", json.dumps(exchange.summary()))

    def test_exchange_rejects_errors_requests_and_wrong_accounts(self):
        for message in (
            {"id": 1, "error": {"message": "secret"}},
            {"id": 99, "method": "secret"},
            {"id": 2, "result": {}},
            {"id": True, "result": {}},
            {"id": 1.0, "result": {}},
            [],
        ):
            exchange = self.module["RefreshExchange"]()
            with self.assertRaises(ValueError):
                exchange.accept(message)
        exchange = self.module["RefreshExchange"]()
        exchange.accept({"id": 1, "result": {}})
        with self.assertRaises(ValueError):
            exchange.accept({"id": 2, "result": {"account": {"type": "apiKey"}}})

    def test_host_version_failure_precedes_auth_read(self):
        root = self.fake_root()
        with self.fake_host(root, version=b"codex-cli 0.164.0\n") as (spawned, opened):
            result = self.module["host_refresh"](root)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(spawned), 1)
        self.assertNotIn("auth.json", opened)

    def test_lock_rejects_nonprivate_file_and_existing_holder(self):
        root = Path(self.home) / "broker"
        root.mkdir(mode=0o700)
        lock = root / "task647-refresh.lock"
        lock.write_text("")
        lock.chmod(0o644)
        with (
            self.temp_lock_open(root),
            self.assertRaises(ValueError),
            self.module["refresh_lock"](root),
        ):
            self.fail("unsafe lock acquired")
        lock.chmod(0o600)
        with (
            self.temp_lock_open(root),
            self.module["refresh_lock"](root),
            self.assertRaises(BlockingIOError),
            self.module["refresh_lock"](root),
        ):
            self.fail("second lock acquired")

    def test_lock_rejects_symlink_and_fifo(self):
        root = Path(self.home) / "broker"
        root.mkdir(mode=0o700)
        lock = root / "task647-refresh.lock"
        lock.symlink_to(root / "missing")
        with (
            self.temp_lock_open(root),
            self.assertRaises(OSError),
            self.module["refresh_lock"](root),
        ):
            self.fail("symlink acquired")
        lock.unlink()
        os.mkfifo(lock, 0o600)
        with (
            self.temp_lock_open(root),
            self.assertRaises(ValueError),
            self.module["refresh_lock"](root),
        ):
            self.fail("fifo acquired")

    def test_transport_is_bounded_and_handles_split_json(self):
        decoder = self.module["BoundedLines"](32, 64)
        self.assertEqual(decoder.feed(b'{"id":'), [])
        self.assertEqual(decoder.feed(b"1}\n"), [{"id": 1}])
        with self.assertRaises(ValueError):
            decoder.feed(b"x" * 33)
        with self.assertRaises(ValueError):
            self.module["BoundedLines"]().feed(b"not json\n")
        decoder = self.module["BoundedLines"]()
        decoder.feed(b"{")
        with self.assertRaises(ValueError):
            decoder.finish()

    def test_expired_or_same_expiry_does_not_qualify(self):
        for exp in (1900000000, 2000000100):
            self.assertEqual(
                self.summary(auth(), auth(exp, "after", "2033-05-18T03:01:00Z"))["status"],
                "incomplete",
            )

    def test_summary_does_not_modify_credentials(self):
        before, after = auth(), auth(2000000200, "after", "2033-05-18T03:01:00Z")
        original = copy.deepcopy((before, after))
        self.summary(before, after)
        self.assertEqual((before, after), original)

    def test_fake_native_rpc_chunks_and_all_messages_remain_private(self):
        messages = [
            {"id": 1, "result": {"codexHome": "fake-secret-home"}},
            {"method": "fake-secret-method", "params": {"secret": "secret"}},
            {
                "id": 2,
                "result": {
                    "account": {"type": "chatgpt", "email": "fake-secret-email"},
                    "requiresOpenaiAuth": True,
                },
            },
        ]
        payload = b"".join(json.dumps(row).encode() + b"\n" for row in messages)
        result, output, process, killed = self.run_transport([payload[:20], payload[20:], b""])
        self.assertIsNone(result["error"])
        self.assertEqual(result["cleanup_status"], "exited")
        self.assertEqual(output, b"")
        killed.assert_not_called()
        sent = [json.loads(row) for row in process.stdin.writes]
        self.assertEqual(
            [row["method"] for row in sent], ["initialize", "initialized", "account/read"]
        )

    def test_fake_transport_failure_kills_owned_group(self):
        for chunks in (
            [b"not-json\n"],
            [b"x" * (1024 * 1024 + 1)],
            [b'{"id":1'],
            [b'{"id":1,"error":{"message":"fake-secret"}}\n'],
        ):
            result, _output, _process, killed = self.run_transport(chunks)
            self.assertEqual(result["error"], "native_failure")
            self.assertEqual(result["cleanup_status"], "terminated")
            self.assertGreaterEqual(killed.call_count, 1)

    def test_fake_transport_timeout_cleans_up(self):
        result, _output, _process, killed = self.run_transport([], timeout=True)
        self.assertEqual(result["error"], "native_failure")
        self.assertGreaterEqual(killed.call_count, 1)

    def test_nonzero_native_exit_is_not_reported_as_forced_termination(self):
        def failed_wait(process, timeout):
            self.assertGreater(timeout, 0)
            process.returncode = 7
            return 7

        with patch.object(FakeProcess, "wait", failed_wait):
            result, _output, _process, killed = self.run_transport(
                [b"codex-cli 0.163.0\n", b""], exchange=False
            )
        self.assertEqual(result["error"], "native_failure")
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["cleanup_status"], "exited")
        killed.assert_not_called()

    def test_fake_version_output_is_bounded(self):
        result, output, _process, killed = self.run_transport(
            [b"codex-cli 0.163.0\n", b""], exchange=False
        )
        self.assertIsNone(result["error"])
        self.assertEqual(output, b"codex-cli 0.163.0\n")
        killed.assert_not_called()
        result, _output, _process, killed = self.run_transport([b"x" * 4097], exchange=False)
        self.assertEqual(result["error"], "native_failure")
        self.assertGreaterEqual(killed.call_count, 1)

    def test_stderr_limit_and_cancellation_cleanup(self):
        result, _output, _process, killed = self.run_transport(
            [], stderr=[b"x" * (1024 * 1024 + 1)]
        )
        self.assertEqual(result["error"], "native_failure")
        self.assertGreaterEqual(killed.call_count, 1)
        process = FakeProcess()
        with (
            patch("os.killpg") as killed,
            patch("time.sleep"),
            patch.object(process, "wait", return_value=0),
        ):
            self.module["stop_native"](process)
        self.assertGreaterEqual(killed.call_count, 2)

    def test_cancelled_transport_never_exports_partial_protocol(self):
        with patch.object(FakeSelector, "select", side_effect=KeyboardInterrupt):
            result, output, _process, killed = self.run_transport([])
        self.assertEqual(result["error"], "cancelled")
        self.assertEqual(result["cleanup_status"], "terminated")
        self.assertEqual(output, b"")
        self.assertGreaterEqual(killed.call_count, 1)

    def test_cleanup_failure_remains_redacted_and_closes_streams(self):
        with patch.object(
            FakeProcess, "wait", side_effect=subprocess.TimeoutExpired("fake-secret", 5)
        ):
            result, _output, _process, killed = self.run_transport([b"malformed\n"])
        self.assertEqual(result["error"], "cleanup_failure")
        self.assertEqual(result["cleanup_status"], "cleanup_failed")
        self.assertGreaterEqual(killed.call_count, 2)

    def test_version_exact_and_private_binary_required(self):
        root = self.fake_root()
        (root / "codex").chmod(0o755)
        with patch("subprocess.Popen") as spawned, self.assertRaises(ValueError):
            self.module["validate_native"](root, {"HOME": str(root / "home")})
        spawned.assert_not_called()

    def test_host_orchestration_reports_rotation_without_model_use(self):
        root = self.fake_root()
        with self.fake_host(root, after=auth(2100000200, "after", "2033-05-18T03:01:00Z")) as (
            spawned,
            opened,
        ):
            result = self.module["host_refresh"](root)
        self.assertEqual(result["status"], "rotation_observed")
        self.assertEqual(result["proactive_request_count"], 1)
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(opened.count("auth.json"), 2)
        self.assertIn("task647-refresh.lock", opened)
        self.assertEqual(len(spawned), 2)
        self.assertEqual(
            spawned[1][0],
            [str(root / "codex"), "-c", self.module["STORE"], "app-server", "--listen", "stdio://"],
        )
        for _argv, kwargs, _process in spawned:
            self.assertEqual(kwargs["cwd"], str(root / "home"))
            self.assertEqual(
                kwargs["env"],
                {
                    "HOME": str(root / "home"),
                    "CODEX_HOME": str(root / "broker"),
                    "PATH": "/usr/bin:/bin",
                },
            )

    def test_failed_protocol_never_upgrades_observed_rotation(self):
        root = self.fake_root()
        with self.fake_host(
            root, after=auth(2100000200, "after", "2033-05-18T03:01:00Z"), rpc_error=True
        ):
            result = self.module["host_refresh"](root)
        self.assertTrue(result["access_changed"])
        self.assertEqual(result["status"], "incomplete")

    def test_cli_host_refresh_requires_explicit_flag_and_emits_one_record(self):
        with (
            patch("sys.argv", ["probe", "host-refresh"]),
            patch("os.open") as opened,
            self.assertRaises(SystemExit),
        ):
            self.module["main"]()
        opened.assert_not_called()
        with (
            patch("sys.argv", ["probe", "--user-run", "--live-root", self.home, "host-refresh"]),
            patch("os.open", side_effect=OSError("fake-secret")),
            patch("builtins.print") as printed,
        ):
            code = self.module["main"]()
        self.assertEqual(code, 1)
        printed.assert_called_once()
        result = json.loads(printed.call_args.args[0])
        self.assertEqual(result["status"], "incomplete")
        self.assertNotIn("secret", json.dumps(result))
