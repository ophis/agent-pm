"""hosts/docker/broker.py: protocol-1 calls, every shared response fixture's outcome, the manifest."""

import io
import json
import os
import stat
import tempfile
from unittest import mock

from agent_pm.session.engine.types import ConfigError, SessionError
from agent_pm.session.hosts.docker import broker
from agent_pm.session.hosts.docker.broker import BROKER, Broker, BrokerError
from agent_pm.session.hosts.docker.types import Started

from session.tests.case import REPO, Case
from session.tests.unit.hosts.docker.support import (
    HOOK,
    FakeBroker,
    FakePopen,
    FakeProc,
    cases,
    data,
    failure,
    success,
    text,
)
from utils.tests.process_fixtures import DEVNULL, PIPE, TimeoutExpired

MANIFEST = "/Users/example/.agent-pm/docker-state/worker_1/0123456789abcdef0123456789abcdef.json"
CONTAINER = "agent-pm-worker_1-0123456789abcdef0123456789abcdef"
KEYS = broker.client_keys(REPO)
CALLS = {
    "version_output": ("version", lambda b: b.version()),
    "build_output": ("build", lambda b: b.build("/opt/agent-pm")),
    "start_output": ("start", lambda b: b.start(MANIFEST, clients=KEYS)),
    "close_output": ("close", lambda b: b.close(CONTAINER)),
}
COMMANDS = {
    "version": CALLS["version_output"],
    "build": CALLS["build_output"],
    "start": CALLS["start_output"],
    "close": CALLS["close_output"],
    "unknown": CALLS["version_output"],
}


def argv(command):
    return [BROKER, *data(f"{command}-input.json")["argv"]]


class PluginRootTest(Case):
    def test_plugin_root_from_hook_path(self):
        self.assertEqual(broker.plugin_root(HOOK), REPO)
        self.assertEqual(KEYS, frozenset({"claude", "codex"}))
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "bin"))
            with self.assertRaisesRegex(ConfigError, "protocol.schema.json"):
                broker.plugin_root(os.path.join(tmp, "bin", "agent-pm-session"))


class ResponseTest(Case):
    def test_every_response_fixture_has_its_outcome(self):
        found = cases()
        self.assertTrue(any(c["definition"] == "error" for c in found))
        for case in (c for c in found if c["expected"]["stage"] == "response"):
            name, definition, valid = case["file"], case["definition"], case["valid"]
            with self.subTest(name=name):
                if definition == "error":
                    command, call = COMMANDS[data(name)["error"]["command"]]
                    fake = FakeBroker(failure(name))
                else:
                    command, call = CALLS[definition]
                    fake = FakeBroker(success(name))
                if definition == "error" and valid:
                    with self.assertRaises(BrokerError) as raised:
                        call(Broker(fake))
                    error = data(name)
                    self.assertEqual(
                        (raised.exception.exit_code, raised.exception.code),
                        (error["exit_code"], error["error"]["code"]),
                    )
                    self.assertEqual(raised.exception.cleanup, error["error"]["cleanup"])
                    self.assertEqual(raised.exception.container, error["error"].get("container"))
                    self.assertIn(error["error"]["message"], str(raised.exception))
                elif valid:
                    call(Broker(fake))
                else:
                    with self.assertRaisesRegex(SessionError, "broker: bad response") as raised:
                        call(Broker(fake))
                    self.assertNotIsInstance(raised.exception, BrokerError)
                self.assertEqual(fake.calls, [argv(command)])
                self.assertEqual(
                    fake.kwargs[0],
                    {
                        "stdin": DEVNULL,
                        "env": {"PATH": os.defpath},
                        "timeout": broker.TIMEOUT,
                    },
                )

    def test_parsed_successes(self):
        self.assertEqual(
            Broker(FakeBroker(success("build-cached.json"))).build("/opt/agent-pm"),
            (data("build-cached.json")["image"], data("build-cached.json")["digest"]),
        )
        started = Broker(FakeBroker(success("start-output.json"))).start(MANIFEST, clients=KEYS)
        self.assertEqual(
            started,
            Started(
                container=CONTAINER,
                endpoint="unix:///Users/example/.docker/run/docker.sock",
                image_digest="sha256:" + "b" * 64,
                clients={"claude": "2.1.0", "codex": "0.100.0"},
            ),
        )
        self.assertFalse(Broker(FakeBroker(success("close-absent.json"))).close(CONTAINER))

    def test_build_passes_a_max_age(self):
        fake = FakeBroker(success("build-cached.json"))
        Broker(fake).build("/opt/agent-pm", max_age=broker.REFRESH)
        self.assertEqual(fake.calls, [[BROKER, *data("build-input-max-age.json")["argv"]]])
        self.assertEqual(broker.REFRESH, 7 * 24 * 3600)

    def test_build_progress_before_the_error_object_is_not_the_error(self):
        progress = "#5 [2/3] RUN apt-get purge -y sudo\n#5 ERROR: process exited 1\n"
        rc, _, err = failure("error-build_failed.json")
        with self.assertRaises(BrokerError) as raised:
            Broker(FakeBroker((rc, "", progress + err))).build("/opt/agent-pm")
        self.assertEqual(raised.exception.code, "build_failed")
        found = Broker(FakeBroker((0, text("build-cached.json"), progress))).build("/opt/agent-pm")
        self.assertEqual(found[0], data("build-cached.json")["image"])
        with self.assertRaises(BrokerError) as raised:
            Broker(FakeBroker((rc, "", "#7 partial\n" + err))).build("/opt/agent-pm")
        self.assertEqual(raised.exception.code, "build_failed")

    def test_only_build_may_put_progress_before_the_error_object(self):
        rc, _, err = failure("error-login_required.json")
        with self.assertRaisesRegex(SessionError, "bad response to start") as raised:
            Broker(FakeBroker((rc, "", "progress\n" + err))).start(MANIFEST, clients=KEYS)
        self.assertNotIsInstance(raised.exception, BrokerError)

    def test_mismatched_exit_is_transport_failure(self):
        for result in (
            failure("error-login_required.json", rc=9),
            (0, "", ""),
            (0, "not json\n", ""),
            (0, '{"protocol_version": "2", "protocol_version": "2"}\n', ""),
            (0, '{"protocol_version": NaN}\n', ""),
            (0, '{"protocol_version": "2"} {}\n', ""),
            (5, "", "not json\n"),
            (5, "", '{"exit_code": true, "error": {}}\n'),
            (
                5,
                "",
                '{"exit_code": 5, "error": {"command": [], "code": [], "message": "m",'
                ' "cleanup": []}}\n',
            ),
            TimeoutExpired(BROKER, broker.TIMEOUT),
        ):
            with self.subTest(result=result):
                with self.assertRaisesRegex(SessionError, "broker: ") as raised:
                    Broker(FakeBroker(result)).version()
                self.assertNotIsInstance(raised.exception, BrokerError)

    def test_wrong_protocol_or_missing_broker_fails_before_build(self):
        for result in (
            (0, '{"protocol_version": "1"}\n', ""),
            (0, '{"protocol_version": "2"}\n', ""),
            FileNotFoundError(2, "No such file", BROKER),
            PermissionError(13, "Permission denied", BROKER),
        ):
            with self.subTest(result=result):
                fake = FakeBroker(result)
                with self.assertRaisesRegex(SessionError, "docs/docker-host.md") as raised:
                    Broker(fake).version()
                self.assertIn(BROKER, str(raised.exception))
                self.assertEqual(fake.calls, [argv("version")])

    def test_a_protocol_2_update_field_is_bad(self):
        started = {**data("start-output.json"), "update": {"status": "updated"}}
        result = (0, json.dumps(started), "")
        with self.assertRaisesRegex(SessionError, "bad response"):
            Broker(FakeBroker(result)).start(MANIFEST, clients=KEYS)

    def test_a_client_key_set_other_than_the_schemas_is_bad(self):
        for keys in (frozenset({"claude"}), frozenset({"claude", "codex", "other"})):
            with self.subTest(keys=keys), self.assertRaisesRegex(SessionError, "bad response"):
                Broker(FakeBroker(success("start-output.json"))).start(MANIFEST, clients=keys)


class ManifestTest(Case):
    def test_writes_the_protocol_manifest_owner_only(self):
        path = os.path.join(self.home, "state", "g.json")
        broker.write_manifest(
            path,
            name="w",
            generation="0" * 32,
            image="agent-pm-worker:" + "a" * 64,
            cwd="/work",
            workspace="agent-pm-crew-w",
            home=("x_dir", "/home/x"),
            hook_path="/plugin/bin/agent-pm-session",
        )
        with open(path) as f:
            self.assertEqual(
                json.load(f),
                {
                    "name": "w",
                    "generation": "0" * 32,
                    "image": "agent-pm-worker:" + "a" * 64,
                    "cwd": "/work",
                    "workspace": "agent-pm-crew-w",
                    "x_dir": "/home/x",
                    "hook_path": "/plugin/bin/agent-pm-session",
                    "labels": {},
                },
            )
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class StreamTest(Case):
    def run_stream(self, out, err, code, **kw):
        popen, shown = FakePopen((out, err, code)), io.StringIO()
        with mock.patch("subprocess.Popen", popen), mock.patch("sys.stderr", shown):
            res = broker._stream(["b"], stdin=DEVNULL, env={"PATH": "/bin"}, timeout=30, **kw)
        return popen, shown.getvalue(), res

    def test_stdout_stays_the_json_and_progress_is_shown(self):
        popen, shown, res = self.run_stream(b'{"ok": 1}\n', b"#1 load\n#2 done\n", 0)
        self.assertEqual(
            (res.returncode, res.stdout, res.stderr), (0, '{"ok": 1}\n', "#1 load\n#2 done\n")
        )
        self.assertEqual(shown, "#1 load\n#2 done\n")
        ((argv, kw, given),) = popen.calls
        self.assertEqual(
            (argv, kw["env"], kw["stdout"], kw["stderr"], given),
            (["b"], {"PATH": "/bin"}, PIPE, PIPE, DEVNULL),
        )

    def test_a_failing_runs_error_object_is_not_shown(self):
        _, shown, res = self.run_stream(b"", b'#1 load\n{"exit_code": 8}\n', 8)
        self.assertEqual((res.returncode, res.stderr), (8, '#1 load\n{"exit_code": 8}\n'))
        self.assertEqual(shown, "#1 load\n")

    def test_build_notes_after_progress_are_shown(self):
        notes = b"#7 partial\nagent-pm-broker: removed agent-pm-user:" + b"1" * 64 + b"\n"
        _, shown, res = self.run_stream(b'{"ok": 1}\n', notes, 0)
        self.assertEqual((res.stdout, shown), ('{"ok": 1}\n', notes.decode()))

    def test_a_broker_reaped_just_after_the_deadline_is_not_a_timeout(self):
        class Late(FakeProc):
            def wait(self, timeout=None):
                if not timeout:
                    raise TimeoutExpired(self.args, 0)
                return self.code

        with mock.patch("subprocess.Popen", return_value=Late(["b"], b"{}\n", b"", 0)):
            res = broker._stream(["b"], stdin=DEVNULL, env={}, timeout=0)
        self.assertEqual((res.returncode, res.stdout), (0, "{}\n"))

    def test_bad_utf8_is_a_decode_error_only_on_stdout(self):
        with self.assertRaises(UnicodeDecodeError):
            self.run_stream(b"\xff", b"", 0)
        _, shown, res = self.run_stream(b"{}\n", b"#3 caf\xe9\n", 0)
        self.assertEqual((res.stderr, shown), ("#3 caf\ufffd\n", "#3 caf\ufffd\n"))

    def test_a_silent_step_shows_its_header_then_times_out_and_is_killed(self):
        proc, shown = FakeProc(["b"], err=b"#5 [2/3] RUN make\n", hang=True), io.StringIO()
        with (
            mock.patch("subprocess.Popen", return_value=proc),
            mock.patch("sys.stderr", shown),
            self.assertRaises(TimeoutExpired),
        ):
            broker._stream(["b"], stdin=DEVNULL, env={}, timeout=0.1)
        self.assertTrue(proc.killed)
        self.assertEqual(shown.getvalue(), "#5 [2/3] RUN make\n")
