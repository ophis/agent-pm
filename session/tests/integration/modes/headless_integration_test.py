"""modes/headless.py over real Python children: the command on pipes, its state, its process group's
kill, bounded waits, and a session running once started.
"""

import contextlib
import io
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from unittest import mock

from agent_pm.session.engine.types import Launch, Reply, SessionError, Spec, Stderr
from agent_pm.session.modes.headless import HeadlessRuntime

from session.tests.fakes.terminal import MemoryHost
from session.tests.headless_support import NOTHING, SID, HeadlessCase, StubAdapter, StubRun

WAIT = 10  # upper bound on any wait, in seconds
SLEEPER = "import time; time.sleep(60)"


def python(code: str) -> tuple[str, ...]:
    return (sys.executable, "-I", "-c", textwrap.dedent(code))


def spawning(then: str = "") -> str:
    """Child code: starts a grandchild running SLEEPER on the child's pipes, prints its pid as
    {"text"}, then runs `then`.
    """
    return (
        "import json, subprocess, sys, time\n"
        f"grandchild = subprocess.Popen([sys.executable, '-I', '-c', {SLEEPER!r}])\n"
        "print(json.dumps({'text': str(grandchild.pid)}), flush=True)\n" + then
    )


def kill(pid: int) -> None:
    """SIGKILL to a test's stray process, if it still runs."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


class Base(HeadlessCase):
    def begin(self, code: str, env: dict | None = None, run=None):
        """self.mode started on this Python running code in self.dir, env added to its child env;
        stopped at cleanup.
        """
        argv = (sys.executable, "-I", "-c", textwrap.dedent(code))
        self.start(StubAdapter(Launch(argv, env=env or {}, cwd=self.dir), run))
        self.addCleanup(self.mode.stop)

    @contextlib.contextmanager
    def spied(self):
        """subprocess.Popen spied: each call's argv in the list."""
        calls, real = [], subprocess.Popen

        def popen(argv, **kwargs):
            calls.append(argv)
            return real(argv, **kwargs)

        with mock.patch("subprocess.Popen", side_effect=popen):
            yield calls

    def drain(self) -> list:
        """Every event up to both pipes' EOF."""
        found, deadline = [], time.monotonic() + WAIT
        while time.monotonic() < deadline:
            events, ended = self.mode.poll(0.1)
            found += events
            if ended:
                return found
        self.fail(f"not ended within {WAIT}s, got {found}")

    def first(self):
        """The next event, polled for up to WAIT s."""
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            events, _ = self.mode.poll(0.1)
            if events:
                return events[0]
        self.fail(f"no event within {WAIT}s")

    def grandchild(self, then: str = "") -> None:
        """begin(spawning(then)) up to the grandchild's pid; the grandchild is killed at cleanup."""
        self.begin(spawning(then))
        self.addCleanup(kill, int(self.first().text))

    def promptly(self, call):
        """call()'s result or exception; fails instead of hanging when it blocks."""
        out = {}

        def run():
            out["value"] = call()

        previous_hook = threading.excepthook

        def capture(args):
            if args.thread is thread:
                out["error"] = args.exc_value
            else:
                previous_hook(args)

        thread = threading.Thread(target=run, daemon=True)
        with mock.patch("threading.excepthook", new=capture):
            thread.start()
            thread.join(WAIT)
        if thread.is_alive():
            self.fail("blocked")
        if "error" in out:
            raise out["error"]
        return out["value"]


class Promptly(Base):
    def test_preserves_original_errors_without_thread_stderr(self):
        for error in (RuntimeError("failed"), KeyboardInterrupt("interrupted")):
            with self.subTest(error=type(error).__name__):

                def fail(*, exception=error):
                    raise exception

                with (
                    mock.patch("sys.stderr", new=io.StringIO()) as console,
                    self.assertRaises(type(error)) as caught,
                ):
                    self.promptly(fail)
                self.assertIs(caught.exception, error)
                self.assertEqual(console.getvalue(), "")

    def test_returns_none_and_restores_the_thread_hook(self):
        hook = threading.excepthook
        self.assertIsNone(self.promptly(lambda: None))
        self.assertIs(threading.excepthook, hook)

    def test_delegates_other_threads_errors_to_the_previous_hook(self):
        error = RuntimeError("other thread")

        def fail():
            raise error

        def run_other():
            other = threading.Thread(target=fail)
            other.start()
            other.join(WAIT)
            self.assertFalse(other.is_alive())

        with mock.patch("threading.excepthook") as previous:
            self.assertIsNone(self.promptly(run_other))
            previous.assert_called_once()
            self.assertIs(previous.call_args.args[0].exc_value, error)
            self.assertIs(threading.excepthook, previous)


class Streams(Base):
    def test_stdout_and_stderr_lines_become_events_before_both_pipes_end(
        self,
    ):
        self.begin(
            """
            import json, os, sys, time
            print(json.dumps({"text": os.getcwd()}))
            print("not json")
            print(json.dumps({"diag": os.environ["HM_PROBE"]}))
            sys.stdout.flush()
            os.close(1)
            time.sleep(0.3)
            sys.stderr.buffer.write(b"bad \\xff\\n")
            sys.exit(3)
        """,
            env={"HM_PROBE": "probe"},
        )
        found = self.drain()
        late = Stderr("bad �")
        self.assertEqual(
            [e for e in found if e != late], [Reply(os.path.realpath(self.dir)), Stderr("probe")]
        )
        self.assertEqual(found.count(late), 1)
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 3)
        self.assertEqual(self.mode.state(), "absent")
        self.mode.stop()
        self.assertEqual(self.mode.returncode(0), 3)

    def test_ended_only_after_stderr_then_stdout(self):
        self.begin("""
            import json, os, sys, time
            sys.stderr.write("first\\n")
            sys.stderr.flush()
            os.close(2)
            time.sleep(0.3)
            print(json.dumps({"text": "late"}))
        """)
        found = self.drain()
        self.assertCountEqual(found, [Stderr("first"), Reply("late")])
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 0)

    def test_an_exception_from_parse_is_raised_from_poll(self):
        self.begin("""
            import time
            print('{"raise": "boom"}', flush=True)
            time.sleep(60)
        """)
        deadline = time.monotonic() + WAIT
        with self.assertRaisesRegex(RuntimeError, "boom"):
            while time.monotonic() < deadline:
                self.mode.poll(0.1)

    def test_parser_failure_stays_single_and_original_when_logging_is_disabled(self):
        error = KeyboardInterrupt("parser interrupted")

        class ErrorRun(StubRun):
            def parse(self, record):
                raise error

        self.addCleanup(logging.disable, logging.root.manager.disable)
        for level in (logging.NOTSET, logging.CRITICAL):
            with self.subTest(level=level), mock.patch("sys.stderr", new=io.StringIO()) as console:
                logging.disable(level)
                self.begin("print('{}')", run=ErrorRun)
                with self.assertRaises(KeyboardInterrupt) as caught:
                    self.drain()
                self.assertIs(caught.exception, error)
                self.mode.poll(0)
                self.mode.stop()
                self.assertEqual(console.getvalue(), "")


class Stop(Base):
    def test_kills_a_sleeping_child_whose_pipes_then_end(self):
        self.begin("import time; time.sleep(60)")
        self.assertEqual(self.mode.state(), "live")
        self.promptly(self.mode.stop)
        self.assertEqual(self.mode.state(), "absent")
        self.assertLess(self.promptly(lambda: self.mode.returncode(WAIT)), 0)
        self.assertEqual(self.drain(), [])
        self.promptly(self.mode.stop)

    def test_kills_the_group_so_a_grandchild_on_the_pipes_ends_them(self):
        self.grandchild("time.sleep(60)\n")
        self.promptly(self.mode.stop)
        self.assertEqual(self.mode.returncode(0), -signal.SIGKILL)
        self.assertEqual(self.drain(), [])

    def test_kills_the_group_after_the_child_exited_while_a_grandchild_holds_the_pipes(self):
        self.grandchild()
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 0)
        self.assertEqual(self.mode.poll(0.2), ([], False))
        self.promptly(self.mode.stop)
        self.assertEqual(self.drain(), [])

    def test_falls_back_to_killing_the_process_when_killpg_fails(self):
        for error in (ProcessLookupError, PermissionError):
            with self.subTest(error=error.__name__):
                self.begin(SLEEPER)
                with mock.patch("os.killpg", side_effect=error) as killpg:
                    self.promptly(self.mode.stop)
                killpg.assert_called_once_with(self.mode.proc.pid, signal.SIGKILL)
                self.assertEqual(self.mode.returncode(0), -signal.SIGKILL)

    def test_waits_at_most_stop_wait_and_ignores_a_vanished_process(self):
        self.begin(SLEEPER)
        self.mode.stop_wait = 0.3
        with (
            mock.patch("os.killpg", side_effect=ProcessLookupError),
            mock.patch.object(self.mode.proc, "kill", side_effect=ProcessLookupError) as killed,
        ):
            began = time.monotonic()
            self.promptly(self.mode.stop)
            self.assertGreaterEqual(time.monotonic() - began, 0.3)
        killed.assert_called_once_with()
        self.assertEqual(self.mode.state(), "live")

    def test_skips_killpg_once_the_child_is_reaped_and_both_pipes_ended(self):
        self.begin("pass")
        self.assertEqual(self.drain(), [])
        self.assertEqual(self.promptly(lambda: self.mode.returncode(WAIT)), 0)
        with mock.patch("os.killpg") as killpg:
            self.promptly(self.mode.stop)
        killpg.assert_not_called()
        self.assertEqual(self.mode.returncode(0), 0)

    def test_is_idempotent_once_started(self):
        self.begin(SLEEPER)
        self.promptly(self.mode.stop)
        self.promptly(self.mode.stop)
        self.assertEqual(self.mode.returncode(0), -signal.SIGKILL)


class Start(Base):
    def test_a_missing_cwd_is_a_driver_error(self):
        with self.assertRaisesRegex(SessionError, rf"\Acannot start {sys.executable}: "):
            self.start(StubAdapter(Launch(NOTHING, cwd=os.path.join(self.dir, "missing"))))
        self.assertEqual((len(self.recorded), self.mode), (1, None))

    def test_start_returns_its_session_running_at_once_with_its_prepared(self):
        with self.spied() as calls:
            mode = HeadlessRuntime.start(
                Spec(workdir=self.dir, sid=SID, client="stub"),
                StubAdapter(Launch(python(SLEEPER))),
                registry=self.session_cls._config().registry,
                record=self.recorded.append,
                host=MemoryHost(),
            )
        self.addCleanup(mode.stop)
        self.assertIsInstance(mode, HeadlessRuntime)
        self.assertEqual(
            (mode.prepared.info, mode.prepared.argv, calls),
            (self.recorded[0], python(SLEEPER), [python(SLEEPER)]),
        )
        self.assertEqual(
            (mode.state(), mode.poll(0), mode.returncode(0)), ("live", ([], False), None)
        )
        self.promptly(mode.stop)
        self.assertEqual((mode.state(), mode.returncode(0)), ("absent", -signal.SIGKILL))


class Returncode(Base):
    def test_none_while_the_child_runs_after_waiting_at_most_timeout(self):
        self.begin(SLEEPER)
        began = time.monotonic()
        self.assertIsNone(self.promptly(lambda: self.mode.returncode(0.3)))
        self.assertGreaterEqual(time.monotonic() - began, 0.3)
        self.assertIsNone(self.promptly(lambda: self.mode.returncode(0)))


if __name__ == "__main__":
    unittest.main()
