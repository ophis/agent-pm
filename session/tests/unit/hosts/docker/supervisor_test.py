"""hosts/docker/supervisor.py: each relay request validated and run as the host hook, its ack, the
dead watch, the capped log and main's lifecycle, with fake processes.
"""

import base64
import fcntl
import io
import json
import os
import signal
import threading
from types import SimpleNamespace
from unittest import mock

from agent_pm.session.hosts.docker import supervisor
from agent_pm.session.hosts.docker.supervisor import Supervisor

from session.tests.case import Case
from utils.tests.process_fixtures import DEVNULL, PIPE, TimeoutExpired

HOOK = "/plugin/bin/agent-pm-session"


def request(body=b'{"session_id": "s", "transcript_path": "/x"}', *, label="Stop", **changes):
    found = {
        "id": 7,
        "argv": ["hook", "/tui/w", label],
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
        self.supervisor = Supervisor(
            "w", self.state, "/tui/w", self.events, HOOK, run=self.hook_run
        )

    def hook_run(self, argv, **kw):
        self.ran.append((argv, kw))
        return SimpleNamespace(returncode=0)

    def log(self):
        try:
            with open(os.path.join(self.state, "supervisor.log")) as f:
                return f.read()
        except FileNotFoundError:
            return ""


class HandleTest(Base):
    def test_a_valid_request_runs_the_hook_without_its_transcript_path(self):
        self.assertEqual(self.supervisor.handle(request()), {"id": 7, "ok": True})
        ((argv, kw),) = self.ran
        self.assertEqual(argv, [HOOK, "hook", "/tui/w", "Stop"])
        self.assertEqual(json.loads(kw["input"]), {"session_id": "s"})
        self.assertEqual(kw["timeout"], supervisor.HOOK_WAIT)
        self.assertEqual(kw["stdout"], kw["stderr"])
        self.assertEqual(self.log(), "")

    def test_anything_else_is_refused_unrun_and_logged(self):
        big = base64.b64encode(b"x" * (supervisor.PAYLOAD_MAX + 1)).decode()
        for line, rid in (
            (b"not json\n", None),
            (b"[]\n", None),
            (request(id=True), None),
            (request(extra=1), 7),
            (request(argv=["hook", "/tui/other", "Stop"]), 7),
            (request(argv=["hook", "/tui/w"]), 7),
            (request(argv="hook"), 7),
            (request(label="a b"), 7),
            (request(label="x" * 65), 7),
            (request(payload="not base64!"), 7),
            (request(payload=7), 7),
            (request(payload=big), 7),
            (request(b"[1]"), 7),
            (request(b"{"), 7),
        ):
            with self.subTest(line=line[:60]):
                self.assertEqual(self.supervisor.handle(line), {"id": rid, "ok": False})
        self.assertEqual(self.ran, [])
        lines = self.log().splitlines()
        self.assertEqual(len(lines), 14)
        self.assertTrue(all(len(line) < 300 for line in lines))

    def test_a_hook_that_cannot_run_or_times_out_is_not_ok(self):
        for error in (OSError(2, "gone"), TimeoutExpired(HOOK, 60)):
            with self.subTest(error=error):
                self.supervisor.run = mock.Mock(side_effect=error)
                self.assertEqual(self.supervisor.handle(request()), {"id": 7, "ok": False})

    def test_the_log_stops_at_its_cap(self):
        with open(os.path.join(self.state, "supervisor.log"), "w") as f:
            f.write("x" * supervisor.LOG_MAX)
        self.supervisor.handle(b"bad\n")
        self.assertEqual(len(self.log()), supervisor.LOG_MAX)


class ServeTest(Base):
    def test_each_line_gets_one_ack_and_an_over_long_line_is_discarded(self):
        self.supervisor.line_max = 80
        requests = io.BytesIO(b"x" * 200 + b"\n" + request(b"{}") + b"bad\n")
        acks = io.BytesIO()
        self.supervisor.serve(requests, acks)
        self.assertEqual(
            [json.loads(line) for line in acks.getvalue().splitlines()],
            [{"id": None, "ok": False}, {"id": 7, "ok": True}, {"id": None, "ok": False}],
        )
        self.assertEqual(len(self.ran), 1)

    def test_once_stopping_it_still_runs_received_hooks_but_writes_no_ack(self):
        self.supervisor.stopping = True
        acks = io.BytesIO()
        self.supervisor.serve(io.BytesIO(request() + request()), acks)
        self.assertEqual((len(self.ran), acks.getvalue()), (2, b""))

    def test_a_closed_ack_stream_is_ignored(self):
        acks = io.BytesIO()
        acks.close()
        self.supervisor.serve(io.BytesIO(request()), acks)
        self.assertEqual(len(self.ran), 1)


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

    def watch(self, *, codes, out=b"1\n", stopped=None):
        results = iter(codes)
        self.supervisor.run = lambda argv, **kw: (
            self.ran.append((argv, kw)) or SimpleNamespace(returncode=next(results))
        )
        self.supervisor.popen = self.popen(out)
        self.supervisor.watch(["wait"], ["check"], stopped or threading.Event())

    def events_text(self):
        with open(self.events) as f:
            return f.read()

    def test_a_dead_pane_writes_one_dead_line(self):
        self.watch(codes=[0])
        self.assertRegex(self.events_text(), r"^\d\d:\d\d:\d\d w dead\n$")
        self.assertEqual([argv for argv, _ in self.ran], [["wait"], ["check"]])

    def test_a_forged_signal_with_a_live_pane_writes_nothing_and_waits_again(self):
        self.watch(codes=[0, 1], out=b"0\n")
        self.assertEqual(self.events_text(), "")
        self.assertEqual([argv for argv, _ in self.ran], [["wait"], ["check"], ["wait"]])

    def test_a_failed_wait_or_a_stop_writes_nothing(self):
        self.watch(codes=[1])
        stopped = threading.Event()
        stopped.set()
        self.watch(codes=[0], stopped=stopped)
        self.supervisor.run = mock.Mock(side_effect=OSError(2, "gone"))
        self.supervisor.watch(["wait"], ["check"], threading.Event())
        self.assertEqual(self.events_text(), "")

    def test_no_events_file_writes_nothing(self):
        self.supervisor.events = ""
        self.watch(codes=[0])
        self.assertEqual(self.events_text(), "")


class MainTest(Base):
    ARGS = ("w", None, "/tui/w", "", HOOK, '["relay"]', '["wait"]', '["check"]')

    def main(self, relay, **patches):
        args = list(self.ARGS)
        args[1] = self.state
        with (
            mock.patch("subprocess.Popen", return_value=relay) as popen,
            mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=1)),
            mock.patch("signal.signal") as handler,
        ):
            code = supervisor.main(args)
        return code, popen, handler

    def test_holds_its_lock_runs_the_relay_and_ends_at_its_eof(self):
        relay = mock.Mock(stdout=io.BytesIO(request(b"{}")), stdin=io.BytesIO())
        code, popen, handler = self.main(relay)
        self.assertEqual(code, 0)
        argv, kw = popen.call_args_list[0]
        self.assertEqual(argv[0], ["relay"])
        self.assertEqual((kw["stdin"], kw["stdout"], kw["stderr"]), (PIPE, PIPE, DEVNULL))
        with open(os.path.join(self.state, "supervisor.pid")) as f:
            self.assertEqual(f.read(), str(os.getpid()))
        (signum, stop), _ = handler.call_args
        self.assertEqual(signum, signal.SIGTERM)
        stop(signal.SIGTERM, None)
        self.assertTrue(relay.stdin.closed)

    def test_a_held_lock_or_bad_args_exit_nonzero(self):
        self.assertEqual(supervisor.main(["too", "few"]), 2)
        fd = os.open(os.path.join(self.state, "supervisor.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, popen, _ = self.main(mock.Mock())
        self.assertEqual(code, 1)
        popen.assert_not_called()
