"""modes/in_process.py: its start from a Spec (checks and the InProcess's check, one session line,
then the run), the run on a daemon thread, its events, a raise as a traceback and code 1, stop's
drain and -SIGKILL, bounded waits, send and screen refused, over real threads.
"""

import dataclasses
import io
import logging
import signal
import threading
import time
import unittest
from unittest import mock

from agent_pm.session.adapters.base import Adapter, InProcess
from agent_pm.session.engine.types import (
    Access,
    ConfigError,
    DriverError,
    Prepared,
    Reply,
    SessionInfo,
    Spec,
    Stderr,
)
from agent_pm.session.modes.in_process import InProcessMode

from session.tests.case import Case
from session.tests.fakes.terminal import MemoryHost

WAIT = 10  # upper bound on any wait, in seconds
SHORT = 0.2  # a timeout that must lapse, in seconds
SPEC = Spec(workdir="/tmp/in-process-test", prompt="hi", sid="s", runtime="stub")
RESUME = Spec(workdir="/tmp/in-process-test", prompt="hi", sid="s", resume=True)


class StubAdapter(Adapter):
    """Its in_process() is a StubRun of `script`, each Spec its check gets kept in `checked`
    (ConfigError: raised).
    """

    forbidden_selection = ("--resume",)
    runtime_note = "STUB NOTE"

    def __init__(self, script, refusal=None):
        super().__init__({})
        self.script, self.refusal, self.checked = script, refusal, []

    def headless(self):
        return None

    def tui(self):
        return None

    def in_process(self):
        return StubRun(self)


class NoInProcess(StubAdapter):
    def in_process(self):
        return None


class StubRun(InProcess):
    """run: its adapter's script(spec)."""

    def __init__(self, adapter: StubAdapter):
        self.adapter = adapter

    def check(self, spec: Spec):
        self.adapter.checked.append(spec)
        if self.adapter.refusal:
            raise self.adapter.refusal

    def run(self, spec: Spec):
        return self.adapter.script(spec)


def ended(*_):
    return [Reply("a")]


class Base(Case):
    def setUp(self):
        self.mode, self.recorded = None, []

    def start(self, adapter, spec=SPEC, record=None, display=None) -> Prepared:
        """self.mode: InProcessMode started on spec with adapter and display, its session line
        given to record, else kept in self.recorded; its Prepared.
        """
        self.mode = InProcessMode.start(
            spec,
            adapter,
            registry=self.driver.registry,
            record=record or self.recorded.append,
            host=MemoryHost(),
            display=display,
        )
        return self.mode.prepared

    def begin(self, script, spec=SPEC):
        self.start(StubAdapter(script), spec)
        self.addCleanup(self.mode.stop)

    def held(self):
        """A script yielding Reply("one"), Stderr("two"), then Reply("late") once released; the
        Events reached, release and closed (its finally ran). Released at cleanup.
        """
        reached, release, closed = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def script(*_):
            try:
                yield Reply("one")
                yield Stderr("two")
                reached.set()
                release.wait(WAIT)
                yield Reply("late")
            finally:
                closed.set()

        return script, reached, release, closed

    def drain(self) -> list:
        """Every item up to the end, polled for up to WAIT s."""
        found, deadline = [], time.monotonic() + WAIT
        while time.monotonic() < deadline:
            items, end = self.mode.poll(0.1)
            found += items
            if end:
                return found
        self.fail(f"not ended within {WAIT}s, got {found}")

    def assert_lapses(self, call, result):
        began = time.monotonic()
        self.assertEqual(call(SHORT), result)
        elapsed = time.monotonic() - began
        self.assertGreaterEqual(elapsed, SHORT)
        self.assertLess(elapsed, SHORT + 1)


class Run(Base):
    def test_items_in_yield_order_then_the_end_and_code_0(self):
        self.begin(lambda *_: iter([Reply("a"), Stderr("b"), Reply("c")]))
        self.assertEqual(self.drain(), [Reply("a"), Stderr("b"), Reply("c")])
        self.assertEqual(self.mode.returncode(WAIT), 0)
        self.assertEqual(self.mode.state(), "absent")

    def test_run_gets_the_spec_on_a_daemon_thread_of_its_own(self):
        calls = []

        def script(*args):
            calls.append((args, threading.get_ident(), threading.current_thread().daemon))
            return []

        self.begin(script, RESUME)
        self.assertEqual(self.drain(), [])
        self.assertEqual(calls, [((RESUME,), mock.ANY, True)])
        self.assertNotEqual(calls[0][1], threading.get_ident())

    def test_poll_and_returncode_wait_at_most_their_timeout_while_the_run_is_live(self):
        script, reached, release, _ = self.held()
        self.begin(script)
        self.assertTrue(reached.wait(WAIT))
        self.assertEqual(
            [self.mode.poll(WAIT) for _ in range(2)],
            [([Reply("one")], False), ([Stderr("two")], False)],
        )
        self.assertEqual(self.mode.state(), "live")
        self.assert_lapses(self.mode.poll, ([], False))
        self.assert_lapses(self.mode.returncode, None)
        self.assertIsNone(self.mode.returncode(0))
        release.set()
        self.assertEqual(self.drain(), [Reply("late")])
        self.assertEqual(self.mode.returncode(WAIT), 0)
        self.assertEqual(self.mode.state(), "absent")


class Raises(Base):
    def test_a_raise_is_its_traceback_as_stderr_lines_then_the_end_and_code_1(self):
        def generator(*_):
            yield Reply("first")
            raise RuntimeError("boom")

        def plain(*_):
            raise RuntimeError("boom")

        for script, before in ((generator, [Reply("first")]), (plain, [])):
            with self.subTest(script=script.__name__):
                self.begin(script)
                found = self.drain()
                self.assertEqual(found[: len(before)], before)
                lines = found[len(before) :]
                self.assertTrue(
                    all(
                        isinstance(e, Stderr) and e.text == e.text.rstrip() and "\n" not in e.text
                        for e in lines
                    ),
                    lines,
                )
                self.assertEqual(lines[0], Stderr("Traceback (most recent call last):"))
                self.assertTrue(any(script.__name__ in e.text for e in lines), lines)
                self.assertEqual(lines[-1], Stderr("RuntimeError: boom"))
                self.assertEqual(self.mode.returncode(WAIT), 1)

    def test_a_keyboard_interrupt_is_code_1(self):
        def script(*_):
            raise KeyboardInterrupt

        self.begin(script)
        self.assertEqual(self.drain()[-1], Stderr("KeyboardInterrupt"))
        self.assertEqual(self.mode.returncode(WAIT), 1)

    def test_failure_stream_is_single_and_silent_when_logging_is_disabled(self):
        def script(*_):
            raise KeyboardInterrupt

        self.addCleanup(logging.disable, logging.root.manager.disable)
        for level in (logging.NOTSET, logging.CRITICAL):
            with self.subTest(level=level), mock.patch("sys.stderr", new=io.StringIO()) as console:
                logging.disable(level)
                self.begin(script)
                found = self.drain()
                self.assertEqual(found.count(Stderr("KeyboardInterrupt")), 1)
                self.assertEqual(self.mode.returncode(WAIT), 1)
                self.assertEqual(console.getvalue(), "")


class Stop(Base):
    def test_live_stop_drains_events_and_closes_run_at_next_yield(
        self,
    ):
        script, reached, release, closed = self.held()
        self.begin(script)
        self.assertTrue(reached.wait(WAIT))
        self.assertEqual(self.mode.state(), "live")
        began = time.monotonic()
        self.mode.stop()
        self.assertEqual(self.drain(), [Reply("one"), Stderr("two")])
        self.assertEqual(self.mode.returncode(WAIT), -signal.SIGKILL)
        self.assertLess(time.monotonic() - began, 1)
        self.assertEqual(self.mode.state(), "absent")
        self.assertFalse(closed.is_set())
        release.set()
        self.assertTrue(closed.wait(WAIT))
        self.assertEqual(self.mode.poll(SHORT), ([], False))
        self.assertEqual(self.mode.returncode(0), -signal.SIGKILL)

    def test_after_the_end_keeps_its_code(self):
        self.begin(ended)
        self.assertEqual(self.drain(), [Reply("a")])
        self.mode.stop()
        self.assertEqual(self.mode.returncode(0), 0)

    def test_is_idempotent(self):
        script, reached, _, _ = self.held()
        self.begin(script)
        self.assertTrue(reached.wait(WAIT))
        self.mode.stop()
        self.mode.stop()
        self.assertEqual(self.drain(), [Reply("one"), Stderr("two")])
        self.assertEqual(self.mode.poll(SHORT), ([], False))
        self.assertEqual(self.mode.returncode(0), -signal.SIGKILL)

    def test_disabled_logging_keeps_stop_bounded_and_suppresses_late_failure(self):
        release, reached = threading.Event(), threading.Event()
        workers = []
        self.addCleanup(release.set)
        self.addCleanup(logging.disable, logging.root.manager.disable)
        logging.disable(logging.CRITICAL)

        def script(*_):
            workers.append(threading.current_thread())
            reached.set()
            release.wait(WAIT)
            raise KeyboardInterrupt

        self.begin(script)
        self.assertTrue(reached.wait(WAIT))
        began = time.monotonic()
        self.mode.stop()
        self.assertLess(time.monotonic() - began, 1)
        self.assertEqual(self.drain(), [])
        self.assertEqual(self.mode.returncode(0), -signal.SIGKILL)
        release.set()
        workers[0].join(WAIT)
        self.assertFalse(workers[0].is_alive())
        self.assertEqual(self.mode.poll(SHORT), ([], False))


class Start(Base):
    def test_its_check_gets_the_spec_before_the_session_line_then_its_run_the_same(self):
        runs = []
        adapter = StubAdapter(lambda spec: runs.append(spec) or [])
        for given, project in (("", False), ("/cwd", True)):
            with self.subTest(cwd=given):
                spec = Spec(
                    workdir="/w",
                    sid="s",
                    runtime="stub",
                    prompt="hi",
                    access=Access(cwd=given, project=project, commands=("git",)),
                )
                checked, lines = len(adapter.checked), []
                prepared = self.start(
                    adapter,
                    spec,
                    record=lambda info, *, lines=lines, checked=checked: lines.append(
                        (info, len(adapter.checked) - checked, len(runs))
                    ),
                )
                self.assertEqual(self.drain(), [])
                noted = adapter.checked[checked]
                info = SessionInfo(
                    sid="s",
                    cwd=given or "/w",
                    project=project,
                    transcript="",
                    resume="",
                )
                self.assertEqual(
                    (noted.prompt, runs[-1], lines),
                    ("hi\n\nSTUB NOTE", noted, [(info, 1, len(runs) - 1)]),
                )
                self.assertEqual(prepared, Prepared(noted, info))

    def test_protect_reaches_check_and_run_without_enforcement(self):
        protected = (self.home, self.home + "/missing")
        runs = []
        adapter = StubAdapter(lambda spec: runs.append(spec) or [])
        prepared = self.start(
            adapter, dataclasses.replace(SPEC, workdir=self.home, protect=protected)
        )
        self.assertEqual(self.drain(), [])
        self.assertIs(prepared.spec.protect, protected)
        self.assertIs(adapter.checked[-1].protect, protected)
        self.assertIs(runs[-1].protect, protected)
        self.assertEqual(len(self.recorded), 1)

    def test_refused_before_its_session_line_or_its_run(self):
        refused = StubAdapter(ended, ConfigError("tier 4 is unmapped"))
        for adapter, fields, message in (
            (NoInProcess(ended), {}, "runtime 'stub' does not support in_process mode"),
            (StubAdapter(ended), {"name": "w1"}, "name: tui mode only"),
            (StubAdapter(ended), {"events": "/e"}, "events: tui mode only"),
            (
                StubAdapter(ended),
                {"flags": ("--resume",)},
                "--resume: picks the session; use sid= or resume=",
            ),
            (StubAdapter(ended), {"sid": None}, "sid: missing"),
            (refused, {}, "tier 4 is unmapped"),
        ):
            with (
                self.subTest(message=message),
                mock.patch("threading.Thread.start", side_effect=AssertionError("run")) as thread,
                self.assertRaises(ConfigError) as caught,
            ):
                self.start(adapter, dataclasses.replace(SPEC, **fields))
            self.assertEqual(str(caught.exception), message)
            thread.assert_not_called()
        with self.assertRaises(ConfigError) as caught:
            self.start(StubAdapter(ended), display="mgr")
        self.assertEqual(str(caught.exception), "display: tui mode only")
        self.assertEqual((self.recorded, len(refused.checked), self.mode), ([], 1, None))

    def test_a_thread_that_cannot_start_is_a_driver_error_after_the_session_line(self):
        calls = []
        with (
            mock.patch(
                "threading.Thread.start", side_effect=RuntimeError("can't start new thread")
            ),
            self.assertRaises(DriverError) as caught,
        ):
            self.start(StubAdapter(lambda *args: calls.append(args) or []))
        self.assertEqual(
            str(caught.exception), "cannot start the in_process run: can't start new thread"
        )
        self.assertEqual((calls, len(self.recorded), self.mode), ([], 1, None))

    def test_start_returns_its_session_running_at_once_with_its_prepared(self):
        script, reached, release, _ = self.held()
        mode = InProcessMode.start(
            SPEC,
            StubAdapter(script),
            registry=self.driver.registry,
            record=self.recorded.append,
            host=MemoryHost(),
        )
        self.addCleanup(mode.stop)
        self.assertIsInstance(mode, InProcessMode)
        self.assertEqual(mode.prepared.info, self.recorded[0])
        self.assertTrue(reached.wait(WAIT))
        self.assertEqual((mode.state(), mode.poll(WAIT)), ("live", ([Reply("one")], False)))
        mode.stop()
        self.assertEqual((mode.state(), mode.returncode(0)), ("absent", -signal.SIGKILL))

    def test_send_and_screen_raise_driver_error(self):
        self.start(StubAdapter(ended))
        for call, message in (
            (lambda: self.mode.send("x"), "send: tui mode only"),
            (self.mode.screen, "screen: tui mode only"),
            (lambda: self.mode.screen(5), "screen: tui mode only"),
        ):
            with self.subTest(message=message), self.assertRaises(DriverError) as caught:
                call()
            self.assertEqual(str(caught.exception), message)


if __name__ == "__main__":
    unittest.main()
