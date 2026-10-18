"""hosts/docker/supervisor.py: each relay request checked against the shared protocol fixtures, its
generation, dir and seq, run as the host hook, its reply and rejected line, the dead watch, the
GitHub token refresher and main's lifecycle on an inherited lock, with fake processes.
"""

import base64
import contextlib
import fcntl
import io
import json
import os
import signal
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

from agent_pm.session.engine.types import SessionError
from agent_pm.session.hosts.docker import supervisor
from agent_pm.session.hosts.docker.supervisor import Supervisor

from session.tests.case import REPO, Case
from utils.tests.process_fixtures import DEVNULL, PIPE, TimeoutExpired

HOOK = "/plugin/bin/agent-pm-session"
GEN = "0123456789abcdef0123456789abcdef"
FIXTURES = os.path.join(REPO, "protocol", "docker-relay", "fixtures")
TUI = "/Users/example/.agent-pm/tui/worker_1"


def request(body=b'{"session_id": "s", "transcript_path": "/x"}', *, label="Stop", **changes):
    found = {
        "id": 7,
        "gen": GEN,
        "seq": 1,
        "argv": ["hook", TUI, label],
        "payload": base64.b64encode(body).decode(),
        **changes,
    }
    return json.dumps(found).encode() + b"\n"


class Base(Case):
    def setUp(self):
        self.state = os.path.join(self.home, "state")
        os.makedirs(self.state)
        self.events = os.path.join(self.home, "events")
        open(self.events, "w").close()
        self.ran = []
        self.code = 0
        self.supervisor = Supervisor(
            "worker_1", self.state, TUI, self.events, HOOK, generation=GEN, run=self.hook_run
        )

    def hook_run(self, argv, **kw):
        self.ran.append((argv, kw))
        return SimpleNamespace(returncode=self.code)

    def rejected(self):
        try:
            with open(os.path.join(self.state, "rejected.jsonl")) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []


class ExpiryTest(Base):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.state, "host.json"), "w") as f:
            json.dump({"generation": GEN, "client": "claude"}, f)

    def test_no_credential_and_a_null_deadline_do_not_close(self):
        self.supervisor.broker = mock.Mock()
        stopped = mock.Mock()
        self.supervisor.expire(stopped)
        with open(os.path.join(self.state, f"{GEN}.credential.json"), "w") as f:
            json.dump(None, f)
        self.supervisor.expire(stopped)
        stopped.wait.assert_not_called()
        self.supervisor.broker.close.assert_not_called()

    def test_invalid_deadline_fails_closed_and_close_failure_retries(self):
        with open(os.path.join(self.state, f"{GEN}.credential.json"), "w") as f:
            f.write('"invalid"')
        self.supervisor.broker = mock.Mock()
        self.supervisor.broker.close.side_effect = [SessionError("unavailable"), False]
        self.supervisor.events = ""
        stopped = mock.Mock()
        stopped.wait.return_value = False
        self.supervisor.expire(stopped)
        self.assertEqual(stopped.wait.call_args_list, [mock.call(0.0), mock.call(60)])
        self.assertEqual(self.supervisor.broker.close.call_count, 2)

    def test_initial_deadline_closes_the_generation_and_reports_repair(self):
        with open(os.path.join(self.state, f"{GEN}.credential.json"), "w") as f:
            json.dump("2026-10-18T12:00:00Z", f)
        self.supervisor.clock = lambda: datetime(2026, 10, 18, 11, 59, tzinfo=UTC).timestamp()
        self.supervisor.broker = mock.Mock()
        stopped = mock.Mock()
        stopped.wait.return_value = False
        self.supervisor.expire(stopped)
        stopped.wait.assert_called_once_with(60.0)
        self.supervisor.broker.close.assert_called_once_with(f"agent-pm-worker_1-{GEN}")
        with open(self.events) as f:
            text = f.read()
        self.assertEqual(
            text.split(" ", 1)[1],
            "worker_1 client_auth_expired Client authentication expired; run agent-pm-broker login"
            " claude; token refresh is not supported yet\n",
        )

    def test_a_missing_or_bad_host_record_still_reports_and_closes(self):
        with open(os.path.join(self.state, f"{GEN}.credential.json"), "w") as f:
            json.dump("2000-01-01T00:00:00Z", f)
        record = os.path.join(self.state, "host.json")
        for content in (None, b"{", b"[]", b'{"generation": "g"}', b'{"client": ""}'):
            with self.subTest(content=content):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(record)
                if content is not None:
                    with open(record, "wb") as f:
                        f.write(content)
                self.supervisor.broker = mock.Mock()
                stopped = mock.Mock()
                stopped.wait.return_value = False
                self.supervisor.expire(stopped)
                self.supervisor.broker.close.assert_called_once_with(f"agent-pm-worker_1-{GEN}")
                with open(self.events) as f:
                    last = f.read().splitlines()[-1]
                self.assertEqual(
                    last.split(" ", 1)[1],
                    "worker_1 client_auth_expired Client authentication expired; run"
                    " agent-pm-broker login for this client; token refresh is not supported yet",
                )

    def test_recovery_after_expiry_is_immediate_and_stop_cancels_close(self):
        with open(os.path.join(self.state, f"{GEN}.credential.json"), "w") as f:
            json.dump("2026-10-18T12:00:00Z", f)
        self.supervisor.clock = lambda: datetime(2026, 10, 18, 12, 1, tzinfo=UTC).timestamp()
        self.supervisor.broker = mock.Mock()
        stopped = mock.Mock()
        stopped.wait.return_value = True
        self.supervisor.expire(stopped)
        stopped.wait.assert_called_once_with(0.0)
        self.supervisor.broker.close.assert_not_called()


class HandleTest(Base):
    def test_relay_fixtures_get_their_expected_reply(self):
        with open(os.path.join(FIXTURES, "index.json")) as f:
            found = [c for c in json.load(f)["cases"] if c["definition"] == "relay_request"]
        for case in found:
            with self.subTest(fixture=case["file"]):
                with open(os.path.join(FIXTURES, case["file"])) as f:
                    data = json.load(f)
                if "payload_bytes" in case:
                    data["payload"] = base64.b64encode(bytes(case["payload_bytes"])).decode()
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.supervisor.seq_file)
                line = json.dumps(data).encode() + b"\n"
                self.assertEqual(self.supervisor.handle(line), case["expected"]["reply"])

    def test_transcript_path_is_dropped_before_the_hook_runs(self):
        self.assertEqual(self.supervisor.handle(request()), {"gen": GEN, "seq": 1, "ok": True})
        ((argv, kw),) = self.ran
        self.assertEqual(argv, [HOOK, "hook", TUI, "Stop"])
        self.assertEqual(json.loads(kw["input"]), {"session_id": "s"})
        self.assertEqual(kw["timeout"], supervisor.HOOK_WAIT)
        self.assertEqual(kw["stdout"], kw["stderr"])
        self.assertEqual(self.rejected(), [])

    def test_malformed_lines_are_rejected_unrun_with_no_gen_seq_or_content(self):
        for line in (
            b"not json\n",
            b"[]\n",
            request(id=True),
            request(extra=1),
            request(gen="X" * 32),
            request(seq=0),
            request(argv=["hook", TUI]),
            request(label="a b"),
            request(payload="not base64!"),
            request(b"[1]"),
            request(b"{"),
        ):
            with self.subTest(line=line[:60]):
                self.assertEqual(self.supervisor.handle(line), {"ok": False})
        self.assertEqual(self.ran, [])
        self.assertEqual(self.rejected(), [{"gen": None, "seq": None, "reason": "malformed"}] * 11)

    def test_old_generation_and_other_dirs_are_rejected_and_logged(self):
        old = "f" * 32
        for line, gen, reason in (
            (request(gen=old), old, "generation"),
            (
                request(argv=["hook", "/Users/example/.agent-pm/tui/other", "Stop"]),
                GEN,
                "directory",
            ),
            (request(argv=["hook", "/Users/example/src/repo/x", "Stop"]), GEN, "directory"),
        ):
            with self.subTest(reason=reason, gen=gen):
                self.assertEqual(self.supervisor.handle(line), {"gen": gen, "seq": 1, "ok": False})
        self.assertEqual(self.ran, [])
        self.assertEqual(
            [r["reason"] for r in self.rejected()], ["generation", "directory", "directory"]
        )
        self.assertFalse(os.path.exists(self.supervisor.seq_file))

    def test_a_seen_seq_is_ok_without_running_the_hook(self):
        self.supervisor.handle(request(seq=2))
        self.assertEqual(len(self.ran), 1)
        for seq in (1, 2):
            with self.subTest(seq=seq):
                self.assertEqual(
                    self.supervisor.handle(request(seq=seq)), {"gen": GEN, "seq": seq, "ok": True}
                )
        self.assertEqual(len(self.ran), 1)
        self.supervisor.handle(request(seq=3))
        self.assertEqual(len(self.ran), 2)

    def test_seq_is_recorded_after_the_hook_runs(self):
        def run(argv, **kw):
            self.assertFalse(os.path.exists(self.supervisor.seq_file))
            return self.hook_run(argv, **kw)

        self.supervisor.run = run
        self.supervisor.handle(request(seq=4))
        with open(self.supervisor.seq_file) as f:
            self.assertEqual(f.read(), "4")

    def test_a_missing_or_corrupt_seq_file_counts_as_0(self):
        with open(self.supervisor.seq_file, "w") as f:
            f.write("junk")
        self.supervisor.handle(request(seq=1))
        self.assertEqual(len(self.ran), 1)

    def test_hook_failure_is_not_ok_logged_and_not_replayed(self):
        self.code = 3
        for seq, error, reason in (
            (1, None, "hook exit 3"),
            (2, OSError(2, "gone"), "hook error"),
            (3, TimeoutExpired(HOOK, 60), "hook timeout"),
        ):
            with self.subTest(reason=reason):
                if error is not None:
                    self.supervisor.run = mock.Mock(side_effect=error)
                self.assertEqual(
                    self.supervisor.handle(request(seq=seq)), {"gen": GEN, "seq": seq, "ok": False}
                )
                self.assertEqual(self.rejected()[-1], {"gen": GEN, "seq": seq, "reason": reason})
        self.supervisor.run = self.hook_run
        self.code = 0
        self.assertEqual(self.supervisor.handle(request(seq=3))["ok"], True)
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(self.supervisor.handle(request(seq=4))["ok"], True)
        self.assertEqual(len(self.ran), 2)

    def test_the_rejected_log_stops_at_its_cap(self):
        path = os.path.join(self.state, "rejected.jsonl")
        with open(path, "w") as f:
            f.write("x" * supervisor.LOG_MAX)
        self.supervisor.handle(b"bad\n")
        self.assertEqual(os.path.getsize(path), supervisor.LOG_MAX)


class ServeTest(Base):
    def test_each_line_gets_one_ack_and_an_over_long_line_is_discarded(self):
        self.supervisor.line_max = 220
        requests = io.BytesIO(b"x" * 500 + b"\n" + request(b"{}") + b"bad\n")
        acks = io.BytesIO()
        self.supervisor.serve(requests, acks)
        self.assertEqual(
            [json.loads(line) for line in acks.getvalue().splitlines()],
            [{"ok": False}, {"gen": GEN, "seq": 1, "ok": True}, {"ok": False}],
        )
        self.assertEqual(len(self.ran), 1)

    def test_once_stopping_it_still_runs_received_hooks_but_writes_no_ack(self):
        self.supervisor.stopping = True
        acks = io.BytesIO()
        self.supervisor.serve(io.BytesIO(request() + request(seq=2)), acks)
        self.assertEqual((len(self.ran), acks.getvalue()), (2, b""))

    def test_a_closed_ack_stream_is_ignored(self):
        acks = io.BytesIO()
        acks.close()
        self.supervisor.serve(io.BytesIO(request()), acks)
        self.assertEqual(len(self.ran), 1)


class Stop:
    def __init__(self, *, stopped=False):
        self.stopped = stopped

    def is_set(self):
        return self.stopped

    def wait(self, _timeout):
        return self.stopped


class WatchTest(Base):
    def popen(self, out):
        def make(argv, **kw):
            self.ran.append((argv, kw))
            read, write = os.pipe()
            os.write(write, out)
            os.close(write)
            stdout = os.fdopen(read, "rb")
            self.addCleanup(stdout.close)
            return mock.MagicMock(stdout=stdout, __enter__=lambda s: s)

        return make

    def watch(self, *, codes, out=b"1\n", stopped=False, start=0.0):
        results = iter(codes)
        self.supervisor.run = lambda argv, **kw: (
            self.ran.append((argv, kw)) or SimpleNamespace(returncode=next(results))
        )
        self.supervisor.popen = self.popen(out)
        self.supervisor.watch(["wait"], ["check"], Stop(stopped=stopped), start)

    def events_text(self):
        with open(self.events) as f:
            return f.read()

    def test_a_dead_pane_writes_one_dead_line(self):
        self.watch(codes=[0])
        self.assertRegex(self.events_text(), r"^\d\d:\d\d:\d\d worker_1 dead\n$")
        self.assertEqual([argv for argv, _ in self.ran], [["wait"], ["check"]])

    def test_a_forged_signal_with_a_live_pane_writes_nothing_and_waits_again(self):
        self.watch(codes=[0, 1], out=b"0\n", start=60)
        self.assertEqual(self.events_text(), "")
        self.assertEqual([argv for argv, _ in self.ran], [["wait"], ["check"], ["wait"]])

    def test_a_failed_wait_is_retried_until_the_containers_tmux_starts(self):
        self.watch(codes=[1, 1, 0], start=60)
        self.assertRegex(self.events_text(), r"^\d\d:\d\d:\d\d worker_1 dead\n$")
        self.assertEqual([argv for argv, _ in self.ran], [["wait"]] * 3 + [["check"]])

    def test_a_failed_wait_or_a_stop_writes_nothing(self):
        self.watch(codes=[1])
        self.watch(codes=[0], stopped=True)
        self.supervisor.run = mock.Mock(side_effect=OSError(2, "gone"))
        self.supervisor.watch(["wait"], ["check"], Stop(), 60)
        self.assertEqual(self.events_text(), "")

    def test_no_events_file_writes_nothing(self):
        self.supervisor.events = ""
        self.watch(codes=[0])
        self.assertEqual(self.events_text(), "")


class Waits:
    """A stop event set after `n` waits; records each timeout."""

    def __init__(self, n):
        self.n, self.timeouts = n, []

    def wait(self, timeout):
        self.timeouts.append(timeout)
        return len(self.timeouts) > self.n


class TokenBroker:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def refresh(self, container, manifest):
        self.calls.append((container, manifest))
        found = self.results.pop(0)
        if isinstance(found, Exception):
            raise found
        return SimpleNamespace(github=found, credential_expires_at=None)


NOW = 1_792_238_400.0  # 2026-10-17T12:00:00Z


def expiry(seconds, account="ophis"):
    at = datetime.fromtimestamp(NOW + seconds, UTC)
    return {"account": account, "expires_at": at.strftime("%Y-%m-%dT%H:%M:%SZ")}


class RefreshTest(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.state, f"{GEN}.github.json")
        self.supervisor.clock = lambda: NOW

    def refresh(self, saved, *results, waits=1):
        if saved is not None:
            with open(self.path, "w") as f:
                json.dump(saved, f)
        self.supervisor.broker = fake = TokenBroker(*results)
        stop = Waits(waits)
        self.supervisor.refresh(stop)
        return fake, stop.timeouts

    def saved(self):
        with open(self.path) as f:
            return json.load(f)

    def test_no_expiry_file_or_an_empty_list_never_refreshes(self):
        for saved in (None, [], "corrupt"):
            with self.subTest(saved=saved):
                fake, timeouts = self.refresh(saved)
                self.assertEqual((fake.calls, timeouts), ([], []))

    def test_it_refreshes_ten_minutes_before_the_earliest_expiry_and_saves_the_new_list(self):
        new = [expiry(3600)]
        fake, timeouts = self.refresh([expiry(3000, "b"), expiry(2400, "a")], new)
        container = f"agent-pm-worker_1-{GEN}"
        self.assertEqual(fake.calls, [(container, os.path.join(self.state, f"{GEN}.json"))])
        self.assertEqual(timeouts, [1800.0, 3000.0])
        self.assertEqual(self.saved(), new)

    def test_a_past_due_expiry_refreshes_at_once(self):
        _, timeouts = self.refresh([expiry(60)], [expiry(3600)])
        self.assertEqual(timeouts[0], 0.0)

    def test_a_failure_writes_the_event_and_retries_every_minute(self):
        new = [expiry(3600)]
        error = SessionError("broker: GitHub request failed: GitHub answered HTTP 500.")
        fake, timeouts = self.refresh([expiry(600)], error, error, new, waits=3)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(timeouts, [0.0, 60.0, 60.0, 3000.0])
        with open(self.events) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertRegex(
                line,
                r"^\d\d:\d\d:\d\d worker_1 github_refresh_failed broker: GitHub request failed:",
            )
        self.assertEqual(self.saved(), new)

    def test_without_an_events_file_a_failure_still_retries(self):
        self.supervisor.events = ""
        fake, timeouts = self.refresh([expiry(600)], SessionError("x"), [], waits=2)
        self.assertEqual((len(fake.calls), timeouts), (2, [0.0, 60.0]))
        self.assertEqual(self.saved(), [])

    def test_a_stop_before_the_due_time_never_refreshes(self):
        fake, timeouts = self.refresh([expiry(3600)], waits=0)
        self.assertEqual((fake.calls, timeouts), ([], [3000.0]))


class MainTest(Base):
    def main(self, relay, fd, *args):
        argv = ["w", self.state, TUI, "", HOOK, GEN, str(fd), '["relay"]', '["wait"]', '["check"]']
        with (
            mock.patch("subprocess.Popen", return_value=relay) as popen,
            mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
            mock.patch("signal.signal") as handler,
            mock.patch("threading.Thread") as self.threads,
        ):
            code = supervisor.main(list(args) or argv)
        return code, popen, handler

    def lock(self):
        fd = os.open(os.path.join(self.state, "supervisor.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        return fd

    def test_main_runs_on_the_inherited_lock_and_ends_at_eof(self):
        fd = self.lock()
        fcntl.flock(fd, fcntl.LOCK_EX)
        relay = mock.Mock(stdout=io.BytesIO(request(b"{}")), stdin=io.BytesIO())
        code, popen, handler = self.main(relay, fd)
        self.assertEqual(code, 0)
        argv, kw = popen.call_args_list[0]
        self.assertEqual(argv[0], ["relay"])
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (PIPE, PIPE, DEVNULL))
        self.assertFalse(os.path.exists(os.path.join(self.state, "supervisor.pid")))
        (signum, stop), _ = handler.call_args
        self.assertEqual(signum, signal.SIGTERM)
        stop(signal.SIGTERM, None)
        self.assertTrue(relay.stdin.closed)
        threads = {
            c.kwargs["target"].__name__: c.kwargs["args"] for c in self.threads.call_args_list
        }
        self.assertEqual(threads.keys(), {"watch", "refresh", "expire"})
        self.assertTrue(threads["refresh"][0].is_set())
        self.assertTrue(threads["expire"][0].is_set())

    def test_an_unheld_lock_fd_or_bad_args_exit_nonzero(self):
        self.assertEqual(supervisor.main(["too", "few"]), 2)
        other = self.lock()
        fcntl.flock(other, fcntl.LOCK_EX)
        mine = os.open(os.path.join(self.state, "supervisor.lock"), os.O_RDWR)
        self.addCleanup(os.close, mine)
        code, popen, _ = self.main(mock.Mock(), mine)
        self.assertEqual(code, 1)
        code, _, _ = self.main(mock.Mock(), 9999)
        self.assertEqual(code, 1)
        popen.assert_not_called()


class CodexRenewalTest(Base):
    def setUp(self):
        super().setUp()
        self.now = NOW
        self.supervisor.clock = lambda: self.now
        self.credential = os.path.join(self.state, f"{GEN}.credential.json")
        self.github = os.path.join(self.state, f"{GEN}.github.json")
        with open(os.path.join(self.state, "host.json"), "w") as stream:
            json.dump({"client": "codex", "generation": GEN}, stream)
        with open(self.github, "w") as stream:
            json.dump([], stream)
        self.supervisor.broker = mock.Mock()
        self.waits = []

    def deadline(self, seconds):
        with open(self.credential, "w") as stream:
            json.dump(expiry(seconds)["expires_at"], stream)

    def stop(self, count):
        def wait(delay):
            self.waits.append(delay)
            if len(self.waits) > count:
                return True
            self.now += delay
            return False

        return SimpleNamespace(wait=wait)

    def renewed(self, client=3600, github=3600):
        self.deadline(client)
        return SimpleNamespace(
            github=[expiry(github)], credential_expires_at=expiry(client)["expires_at"]
        )

    def test_earliest_client_or_github_deadline_schedules_refresh(self):
        for client, github, delay in ((1800, 3600, 1200), (3600, 1800, 1200)):
            with self.subTest(client=client):
                self.now, self.waits = NOW, []
                self.deadline(client)
                with open(self.github, "w") as stream:
                    json.dump([expiry(github)], stream)
                self.supervisor.broker.refresh.side_effect = lambda *_args: self.renewed(7200, 7200)
                self.supervisor.refresh(self.stop(1))
                self.assertEqual(self.waits, [delay, 5400])
                self.supervisor.broker.close.assert_not_called()

    def test_expired_recovery_reconciles_before_closing(self):
        self.deadline(-1)
        self.supervisor.broker.refresh.side_effect = lambda *_args: self.renewed()
        self.supervisor.refresh(self.stop(1))
        self.assertEqual(self.waits, [0, 3000])
        self.supervisor.broker.close.assert_not_called()

    def test_failure_retries_until_expiry_then_full_event_and_log_and_close(self):
        self.deadline(100)
        self.supervisor.broker.refresh.side_effect = SessionError("offline")
        self.supervisor.broker.close.side_effect = [SessionError("offline"), False]
        with self.assertLogs("session", level="WARNING") as logged:
            self.supervisor.refresh(self.stop(4))
        reason = "Client authentication expired; run /usr/local/libexec/agent-pm/agent-pm-broker login codex on the host, then resume"
        self.assertEqual(self.waits, [0, 60, 40, 60])
        self.assertEqual(self.supervisor.broker.refresh.call_count, 3)
        self.assertEqual(self.supervisor.broker.close.call_count, 2)
        with open(self.events) as stream:
            lines = stream.read().splitlines()
        self.assertEqual(lines[-1].split(" ", 1)[1], f"worker_1 client_auth_expired {reason}")
        self.assertIn(
            f"WARNING:session:docker session worker_1: client_auth_expired {reason}", logged.output
        )

    def test_partial_publication_survives_lost_reply_and_github_failure(self):
        self.deadline(30)

        def partial(*_args):
            self.deadline(120)
            raise SessionError("GitHub unavailable")

        self.supervisor.broker.refresh.side_effect = partial
        self.supervisor.refresh(self.stop(1))
        self.assertEqual(self.waits, [0, 60])
        self.supervisor.broker.close.assert_not_called()

    def test_successful_short_lifetime_has_no_busy_loop(self):
        self.deadline(30)
        self.supervisor.broker.refresh.side_effect = lambda *_args: self.renewed(120)
        self.supervisor.refresh(self.stop(1))
        self.assertEqual(self.waits, [0, 60])

    def test_missing_or_bad_deadline_reconciles_and_fails_closed(self):
        for value in (None, "bad", []):
            with self.subTest(value=value):
                self.waits = []
                with open(self.credential, "w") as stream:
                    json.dump(value, stream)
                self.supervisor.broker.refresh.side_effect = SessionError("unavailable")
                self.supervisor.broker.close.reset_mock()
                self.supervisor.refresh(self.stop(1))
                self.supervisor.broker.close.assert_called_once_with(f"agent-pm-worker_1-{GEN}")

    def test_stop_cancels_due_refresh_and_old_expiry_owner(self):
        self.deadline(30)
        self.supervisor.refresh(self.stop(0))
        self.supervisor.expire(self.stop(0))
        self.supervisor.broker.refresh.assert_not_called()
        self.supervisor.broker.close.assert_not_called()
        self.assertEqual(self.waits, [0])
