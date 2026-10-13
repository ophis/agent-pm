"""Lifecycle behavior."""

import dataclasses
import os
import re
import signal
import subprocess
import time
from dataclasses import replace
from datetime import datetime
from unittest import mock

from agent_pm.delegate import (
    Begun,
    ConfigError,
    Finished,
    Limit,
    Progress,
    Result,
)
from agent_pm.delegate.engine import runner
from agent_pm.delegate.engine.runner import FIX, IDLE_TIMEOUT, POLICIES
from agent_pm.delegate.engine.types import Stopped
from agent_pm.session import (
    MODES,
)
from agent_pm.utils import printable

from delegate.tests.replies import bare, block, running
from delegate.tests.unit.engine.runner.case import INVALID, INVALID_ERROR, Base, ended
from delegate.tests.unit.runs import (
    DONE,
    START,
    limit,
    no_child,
    said,
)


class Configures(Base):
    def test_the_callers_settings_reach_the_run(self):
        r = self.go(said(START, block(DONE)))
        self.assertEqual((r.outcome.status, len(self.launches())), ("done", 1))

    def test_a_fresh_driver_without_the_runtime_starts_nothing(self):
        self.configure_session({})
        with self.assertRaisesRegex(ConfigError, r"\[adapters.fake\]"):
            self.go(said(START, block(DONE)))
        self.assertFalse(os.path.exists(self.log))


class Callbacks(Base):
    def test_begun_then_a_progress_per_mark_then_finished(self):
        r = self.go(
            said(START, "Working.", "[agent-pm-progress:round] half way"), said(block(DONE))
        )
        sid = self.records()[0]["sid"]
        self.assertEqual(
            self.events,
            [Begun(sid), Progress("start", "go"), Progress("round", "half way"), Finished(r)],
        )
        self.assertEqual((r.returncode, r.outcome.status, r.error, r.limit), (0, "done", "", None))

    def test_a_turn_end_repeating_the_last_reply_is_read_once(self):
        text = "\n".join((START, block(DONE)))
        r = self.go(said(text), ended(text), ended(""))
        self.assertEqual(
            ([type(e) for e in self.events], r.outcome.status),
            ([Begun, Progress, Finished], "done"),
        )
        self.assertEqual((self.kinds().count("progress"), self.err.count(START)), (1, 1))

    def test_a_differing_turn_end_is_a_reply(self):
        r = self.go(said(START), ended("[agent-pm-progress:round] two\n" + block(DONE)))
        self.assertEqual(self.events[1:3], [Progress("start", "go"), Progress("round", "two")])
        self.assertEqual(r.outcome.status, "done")

    def test_a_deliverable_file_is_read_in_and_saved_to_out(self):
        self.write(os.path.join(self.work, "notes", "doc.md"), "# From a file\n")
        data = {k: v for k, v in DONE.items() if k != "deliverable"} | {
            "deliverable_file": "notes/doc.md"
        }
        r = self.go(said(START, block(data)))
        self.assertEqual(
            (r.outcome.status, r.outcome.deliverable, r.outcome.url),
            ("done", "# From a file\n", self.out),
        )
        self.assertEqual(self.read(self.out), "# From a file\n")
        fields = {f.name for f in dataclasses.fields(r.outcome)} - {"deliverable"}
        self.assertEqual(set(self.last("result")["outcome"]), fields)


class Fix(Base):
    """FR-21: one fix resume of the same session after an invalid outcome block, only when the
    client exited 0.
    """

    def test_an_invalid_block_is_fixed_by_one_resume_of_the_session(self):
        r = self.go(said(START, block(INVALID)), fixes=[said(block(DONE))])
        self.assertEqual((r.returncode, r.outcome.status, r.error), (0, "done", ""))
        first, second = (launch["argv"] for launch in self.launches())
        sid = self.events[0].sid
        self.assertEqual((first[first.index("--sid") + 1], "--resume" in first), (sid, False))
        self.assertEqual((second[second.index("--sid") + 1], "--resume" in second), (sid, True))
        self.assertEqual(second[second.index("--") + 1], FIX.format(error=INVALID_ERROR))
        self.assertEqual([type(e) for e in self.events], [Begun, Progress, Finished])
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )
        self.assertEqual(self.last("invalid")["error"], INVALID_ERROR)
        self.assertIn(f"delegate: invalid outcome: {INVALID_ERROR}; resuming once\n", self.err)
        self.assertEqual(self.read(self.out), "# Doc\n")

    def test_an_invalid_block_twice_is_the_error(self):
        r = self.go(said(START, block(INVALID)), fixes=[said(block(INVALID))])
        self.assertEqual(r, Result(0, None, f"invalid outcome: {INVALID_ERROR}"))
        self.assertEqual(
            (len(self.launches()), [type(e) for e in self.events]), (2, [Begun, Progress])
        )
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )
        self.assertEqual(bare(self.last("result")), {"kind": "result", "error": r.error})

    def test_no_block_is_the_error_without_a_resume(self):
        r = self.go(said(START, "bye"))
        self.assertEqual(
            (r, len(self.launches())), (Result(0, None, "the agent run returned no outcome"), 1)
        )

    def test_an_invalid_block_from_a_failed_client_is_not_resumed(self):
        r = self.go(said(START, block(INVALID)), {"exit": 3})
        self.assertEqual((r, len(self.launches())), (Result(3, None, "the client exited 3"), 1))
        self.assertNotIn("invalid", self.kinds())

    def test_a_fix_resume_that_cannot_start_is_the_error(self):
        real, calls = subprocess.Popen, []

        def popen(*args, **kw):
            calls.append(args)
            if len(calls) > 1:
                raise FileNotFoundError(2, "No such file or directory", "x")
            return real(*args, **kw)

        with mock.patch("subprocess.Popen", popen):
            r = self.go(said(START, block(INVALID)))
        self.assertEqual((r.returncode, r.outcome), (1, None))
        self.assertTrue(
            r.error.startswith(f"invalid outcome: {INVALID_ERROR}; resume failed: cannot start "),
            r.error,
        )
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )


class Idle(Base):
    def test_silence_longer_than_the_idle_timeout_is_recorded_and_the_run_goes_on(self):
        self.assertEqual(IDLE_TIMEOUT, 2 * 60 * 60)
        start = self.driver.start
        with mock.patch.object(
            self.driver,
            "start",
            side_effect=lambda *a, **kw: start(
                *a, options=replace(kw["options"], idle_timeout=0.3)
            ),
        ):
            r = self.go({"sleep": 1.0}, said(START, block(DONE)))
        self.assertEqual(r.outcome.status, "done")
        idle = [e for e in self.records() if e["kind"] == "idle"]
        self.assertTrue(idle)
        self.assertEqual({e["seconds"] for e in idle}, {0.3})


class UsageLimit(Base):
    """FR-58: a usage limit stops the session and ends the run: no outcome read, no fix resume, no
    Finished.
    """

    def test_a_limit_stops_the_session_and_is_the_result(self):
        at = 1767225600.0
        began = time.monotonic()
        r = self.go(said(START), limit(at), {"sleep": 30}, said(block(DONE)))
        self.assertLess(time.monotonic() - began, 20)
        self.assertFalse(running(self.launches()[0]["pid"]))
        when = datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")
        self.assertEqual(
            r, Result(-signal.SIGKILL, None, f"usage limit; resets at {when}", Limit(at))
        )
        self.assertEqual(r.limit.resets_at, at)
        self.assertEqual(self.kinds(), ["session", "input", "progress", "limit", "result", "end"])
        self.assertEqual((self.last("limit")["resets_at"], len(self.launches())), (at, 1))
        self.assertEqual([type(e) for e in self.events], [Begun, Progress])

    def test_an_unknown_reset_time(self):
        r = self.go(said(START, block(INVALID)), limit(None))
        self.assertEqual(
            (r.outcome, r.error, r.limit), (None, "usage limit; reset time unknown", Limit(None))
        )
        self.assertEqual((self.last("limit")["resets_at"], len(self.launches())), (None, 1))


class Stops(Base):
    """
    Main's HostFailure cases: a session stopped on an exception, its record closed `stopped: …`.
    """

    def test_an_on_event_raise_stops_the_session_and_propagates(self):
        failure = RuntimeError("boom")

        def boom(event):
            if isinstance(event, Progress):
                raise failure

        with self.assertRaises(RuntimeError) as caught:
            self.go(said(START), {"sleep": 30}, said(block(DONE)), on_event=boom)
        self.assertIs(caught.exception, failure)
        self.assertFalse(running(self.launches()[0]["pid"]))
        result, end = self.records()[-2:]
        self.assertEqual((result["error"], end["rc"]), ("stopped: RuntimeError: boom", 1))

    def test_an_exception_ends_the_record_stopped(self):
        for error, rc, text in (
            (Stopped(signal.SIGTERM), 143, "stopped: Stopped: 143"),
            (SystemExit(129), 129, "stopped: SystemExit: 129"),
            (SystemExit("bye"), 1, "stopped: SystemExit: bye"),
            (KeyboardInterrupt(), 1, "stopped: KeyboardInterrupt"),
            (RuntimeError("one\ntwo"), 1, "stopped: RuntimeError: one two"),
        ):

            def raise_(event, error=error):
                if isinstance(event, Progress):
                    raise error

            with self.subTest(error=error):
                self.fresh()
                with self.assertRaises(type(error)) as cm:
                    self.go(said(START), {"sleep": 30}, on_event=raise_)
                self.assertIs(cm.exception, error)
                self.assertFalse(running(self.launches()[-1]["pid"]))
                self.assertEqual(self.kinds(), ["session", "input", "progress", "result", "end"])
                result, end = self.records()[-2:]
                self.assertEqual(
                    (bare(result), bare(end)),
                    (
                        {"kind": "result", "error": text},
                        {"kind": "end", "sid": end["sid"], "rc": rc},
                    ),
                )

    def test_the_closing_writes_never_mask_the_runs_exception(self):
        def swap(_event):
            os.remove(self.channel)
            os.mkdir(self.channel)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.go({"sleep": 30}, on_event=swap)
        self.assertEqual(len(re.findall(r"^delegate: .*run\.jsonl", self.err, re.M)), 2, self.err)

    def test_a_spawn_failure_is_the_result(self):
        with mock.patch(
            "subprocess.Popen", side_effect=FileNotFoundError(2, "No such file or directory", "x")
        ):
            r = self.go(said(START, block(DONE)))
        self.assertEqual((r.returncode, r.outcome, self.events), (1, None, []))
        self.assertTrue(r.error.startswith("headless: cannot start "), r.error)
        self.assertEqual(self.kinds(), ["session", "input", "result", "end"])
        self.assertEqual((self.last("result")["error"], self.last("end")["rc"]), (r.error, 1))

    def test_a_driver_refusal_is_a_config_error_and_nothing_is_recorded(self):
        with self.assertRaises(ConfigError) as cm:
            self.go(said(START, block(DONE)), runtime="nope")
        self.assertIn("nope", str(cm.exception))
        self.assertEqual(
            (os.path.lexists(self.channel), os.path.lexists(self.log), self.events),
            (False, False, []),
        )


class InProcess(Base):
    """The in_process-only fake, its run with no child process."""

    def setUp(self):
        super().setUp()
        self.configure_session(
            {
                "fake": {
                    "implementation": "adapters.fake:InProcessFakeAdapter",
                    "scenario": self.scenario,
                    "log": self.log,
                }
            }
        )

    def go(self, *steps, **kw) -> Result:
        with no_child():
            return super().go(*steps, mode="in_process", **kw)

    def test_a_policy_for_each_mode_in_process_the_headless_one(self):
        self.assertEqual(set(POLICIES), set(MODES))
        self.assertIs(POLICIES["in_process"], POLICIES["headless"])

    def test_a_done_run(self):
        r = self.go(said(START, block(DONE)))
        sid = self.events[0].sid
        self.assertEqual(self.events, [Begun(sid), Progress("start", "go"), Finished(r)])
        self.assertEqual((r.returncode, r.outcome.status, r.error), (0, "done", ""))
        self.assertEqual(self.kinds(), ["session", "input", "progress", "result", "end"])
        self.assertEqual((self.read(self.out), self.launches()[0]["pid"]), ("# Doc\n", os.getpid()))

    def test_a_raise_is_the_client_exiting_1_its_traceback_on_stderr(self):
        r = self.go(said(START), {"raise": "boom"})
        self.assertEqual(r, Result(1, None, "the client exited 1"))
        self.assertEqual(
            ([type(e) for e in self.events], self.last("end")["rc"]), ([Begun, Progress], 1)
        )
        lines = self.err.splitlines()
        self.assertLess(
            lines.index("Traceback (most recent call last):"), lines.index("RuntimeError: boom")
        )
        r = self.go(said(START, block(DONE)), {"raise": "boom"})
        self.assertEqual(
            (r.returncode, r.outcome.status, r.error), (1, "done", "the client exited 1")
        )

    def test_silence_longer_than_the_idle_timeout_is_recorded_and_the_run_goes_on(self):
        start = self.driver.start
        with mock.patch.object(
            self.driver,
            "start",
            side_effect=lambda *a, **kw: start(
                *a, options=replace(kw["options"], idle_timeout=0.3)
            ),
        ):
            r = self.go({"sleep": 1.0}, said(START, block(DONE)))
        self.assertEqual(r.outcome.status, "done")
        idle = [e for e in self.records() if e["kind"] == "idle"]
        self.assertTrue(idle)
        self.assertEqual({e["seconds"] for e in idle}, {0.3})

    def test_an_invalid_block_is_fixed_by_one_resume_of_the_session(self):
        r = self.go(said(START, block(INVALID)), fixes=[said(block(DONE))])
        self.assertEqual((r.returncode, r.outcome.status, r.error), (0, "done", ""))
        first, second = self.launches()
        sid = self.events[0].sid
        self.assertEqual(
            (first["sid"], first["resume"], second["sid"], second["resume"]),
            (sid, False, sid, True),
        )
        self.assertEqual(second["prompt"], FIX.format(error=INVALID_ERROR))
        self.assertEqual(
            self.kinds(), ["session", "input", "progress", "invalid", "session", "result", "end"]
        )


class Terminal(Base):
    """Main's Sinks case: what reaches the terminal is printable."""

    def test_printable_is_the_utils_one(self):
        self.assertIs(runner.printable, printable)

    def test_replies_progress_and_stderr_lines_are_printed_printable(self):
        self.go(
            {"err": "w\x1b[2Jarn"},
            said(
                "a\x1b]52;c;ZXZpbA==\x07b\tc",
                "",
                "[agent-pm-progress:round] x\x1b[2J",
                START,
                block(DONE),
            ),
        )
        lines = self.err.splitlines()
        for want in (
            "w[2Jarn",
            "a]52;c;ZXZpbA==b\tc",
            "[agent-pm-progress:round] x[2J",
            "Progress (round): x[2J",
            "Progress (start): go",
        ):
            self.assertIn(want, lines)
        self.assertLess(lines.index("a]52;c;ZXZpbA==b\tc"), lines.index("Progress (round): x[2J"))
        self.assertNotIn("", lines)
        self.assertFalse([c for c in self.err if not c.isprintable() and c not in "\t\n"])
