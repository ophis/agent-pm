"""modes/in_process.py: an InProcess's run on a daemon thread, its events, a raise as a traceback
and code 1, stop's drain and -SIGKILL, bounded waits, send and screen refused, over real threads.
"""

import io
import logging
import signal
import threading
import time
import unittest
from unittest import mock

from agent_pm.session.adapters.types import Adapter, InProcess
from agent_pm.session.engine.types import DriverError, Reply, SessionRequest, Spec, Stderr
from agent_pm.session.modes.in_process.types import InProcessMode

from session.tests.case import Case

WAIT = 10  # upper bound on any wait, in seconds
SHORT = 0.2  # a timeout that must lapse, in seconds
SPEC = Spec(workdir="/tmp/in-process-test")


class StubAdapter(Adapter):
    def headless(self):
        return None

    def tui(self):
        return None


class StubRun(InProcess):
    """run: script(prompt, spec, resume)."""

    def __init__(self, script):
        super().__init__(StubAdapter({}))
        self.script = script

    def run(self, prompt, request: SessionRequest):
        spec = request.spec
        resume = request.resumed
        return self.script(prompt, spec, resume)


def ended(*_):
    return [Reply("a")]


class Base(Case):
    def begin(self, script, *, resume=False):
        self.mode = InProcessMode(StubRun(script))
        self.mode.begin("hi", SessionRequest(SPEC, resumed=resume))
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

    def test_run_gets_prompt_spec_and_resume_on_a_daemon_thread_of_its_own(self):
        calls = []

        def script(*args):
            calls.append((args, threading.get_ident(), threading.current_thread().daemon))
            return []

        self.begin(script, resume=True)
        self.assertEqual(self.drain(), [])
        self.assertEqual(calls, [(("hi", SPEC, True), mock.ANY, True)])
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

    def test_is_a_no_op_before_begin(self):
        mode = InProcessMode(StubRun(ended))
        mode.stop()
        self.assertEqual(mode.state(), "absent")
        self.assertIsNone(mode.returncode(0))
        self.mode = mode
        mode.begin("hi", SessionRequest(SPEC, resumed=False))
        self.addCleanup(mode.stop)
        self.assertEqual(self.drain(), [Reply("a")])
        self.assertEqual(mode.returncode(WAIT), 0)

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


class Begin(Base):
    def test_a_thread_that_cannot_start_is_a_driver_error(self):
        calls = []
        mode = InProcessMode(StubRun(lambda *args: calls.append(args) or []))
        with (
            mock.patch(
                "threading.Thread.start", side_effect=RuntimeError("can't start new thread")
            ),
            self.assertRaises(DriverError) as caught,
        ):
            mode.begin("hi", SessionRequest(SPEC, resumed=False))
        self.assertEqual(
            str(caught.exception), "cannot start the in_process run: can't start new thread"
        )
        self.assertEqual(calls, [])
        self.assertEqual(mode.state(), "absent")
        mode.stop()

    def test_send_and_screen_raise_driver_error_before_and_after_begin(self):
        self.mode = InProcessMode(StubRun(ended))
        for when in ("before", "after"):
            if when == "after":
                self.mode.begin("hi", SessionRequest(SPEC, resumed=False))
            for call, message in (
                (lambda: self.mode.send("x"), "send: tui mode only"),
                (self.mode.screen, "screen: tui mode only"),
                (lambda: self.mode.screen(5), "screen: tui mode only"),
            ):
                with (
                    self.subTest(when=when, message=message),
                    self.assertRaises(DriverError) as caught,
                ):
                    call()
                self.assertEqual(str(caught.exception), message)


if __name__ == "__main__":
    unittest.main()
