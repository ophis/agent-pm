"""Pure logic over fake objects and in-process adapters."""

import io
import os
import signal
from contextlib import redirect_stderr
from datetime import datetime
from unittest import mock

from agent_pm.delegate import Begun, ConfigError, Finished, Limit, Progress, Result, plan, run
from agent_pm.delegate.engine.runner import FIX, IDLE_TIMEOUT, NUDGE, POLICIES, STOP_LIMIT
from agent_pm.delegate.event.reply import marks
from agent_pm.session import Activity, Blocked, DriverError, Exited, IdleTimeout, LimitHit, Mode

from delegate.tests.replies import bare, block
from delegate.tests.runner import INVALID, INVALID_ERROR, LEFT_OPEN, PENDING, SID, Base
from delegate.tests.unit.engine.instruction import ROLES, InstructionCase
from delegate.tests.unit.runs import (
    DONE,
    GAVE_UP,
    NAME,
    START,
    STOP,
    FakeHandle,
    no_child,
    said,
    turn,
)


class InProcess(Base):
    """The in_process-only fake, its run with no child process."""

    def setUp(self):
        super().setUp()
        self.configure_session(
            {
                "fake": {
                    "implementation": "agent_pm.session.adapters.fake:InProcessFakeAdapter",
                    "scenario": self.scenario,
                    "log": self.log,
                }
            }
        )

    def go(self, *steps, **kw) -> Result:
        with no_child():
            return super().go(*steps, mode="in_process", **kw)

    def test_a_policy_for_each_mode_in_process_the_headless_one(self):
        self.assertEqual(set(POLICIES), set(Mode))
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
            side_effect=lambda *a, **kw: start(*a, **{**kw, "idle_timeout": 0.3}),
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


class TuiPolicy(Base):
    """The tui policy over a FakeHandle (start patched as delegate.engine.runner sees it)."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("TUI_ATTACH_PREFIX", None)

    def faked(self, *events, fail=None, stop_fail=None, mode="tui", **kw) -> Result:
        self.handle = FakeHandle(events, fail, stop_fail)
        with mock.patch.object(self.driver, "start", side_effect=self.handle.start) as self.started:
            return self.go(mode=mode, **kw)

    def test_the_policies_values(self):
        self.assertEqual(STOP_LIMIT, 3)
        self.assertEqual(
            NUDGE,
            "Finish your task, then end your last reply with your outcome block "
            "(`agent-pm-outcome`), as your instructions' Return says. If you are waiting for "
            "background work, wait for it first.",
        )

    def test_a_tui_handle_without_a_name_reports_a_driver_error(self):
        os.makedirs(self.work)
        handle = FakeHandle([])
        handle.name = None
        with mock.patch.object(self.driver, "start", return_value=handle):
            result = self.go(mode="tui")
        self.assertEqual((result.returncode, result.error), (1, "tui: tui session has no name"))

    def test_start_hands_the_plan_to_the_driver_and_prints_the_attach_command(self):
        events = os.path.join(self.tmp, "events.log")
        for kw, want, name in (
            ({}, ({"resume": None, "events": None}, None), NAME),
            (
                {"sid": SID, "resume": True, "name": "mine", "events": events},
                ({"resume": SID, "events": events}, "mine"),
                "mine",
            ),
        ):
            with self.subTest(kw=kw):
                self.fresh()
                r = self.faked(turn(START, block(DONE)), **kw)
                args, given = self.started.call_args
                self.assertEqual(
                    ((args[0].session.runtime, args[0].session.mode), r.outcome.status),
                    (("fake", "tui"), "done"),
                )
                self.assertEqual(
                    (
                        given,
                        args[0].session.sid,
                        args[0].session.name,
                        args[0].session.resume,
                        args[0].session.events,
                    ),
                    (
                        {"idle_timeout": IDLE_TIMEOUT},
                        self.events[0].sid,
                        want[1],
                        want[0]["resume"] is not None,
                        want[0]["events"],
                    ),
                )
                self.assertIn(f"delegate: tui session {name}: tmux attach -t '={name}'\n", self.err)

    def test_a_valid_outcome_at_a_turn_end_is_done_and_leaves_the_session_even_with_work_pending(
        self,
    ):
        for pending in (0, 2):
            with self.subTest(pending=pending):
                self.fresh()
                r = self.faked(turn(START, block(DONE), pending=pending))
                self.assertEqual(
                    (r.returncode, r.outcome.status, r.error, self.handle.calls),
                    (0, "done", "", []),
                )
                self.assertEqual(
                    self.events, [Begun(self.events[0].sid), Progress("start", "go"), Finished(r)]
                )
                self.assertEqual((self.read(self.out), self.last("end")["rc"]), ("# Doc\n", 0))
                self.assertEqual(self.kinds(), ["input", "progress", "result", "end"])

    def test_the_first_turn_end_without_an_outcome_nudges_once(self):
        r = self.faked(turn(START), STOP, turn(block(DONE)))
        self.assertEqual((r.outcome.status, self.handle.calls), ("done", [("send", NUDGE)]))
        self.assertEqual(self.kinds(), ["input", "progress", "nudge", "result", "end"])
        self.assertEqual(bare(self.last("nudge")), {"kind": "nudge"})

    def test_it_gives_up_after_stop_limit_counted_turn_ends_and_leaves_the_session(self):
        r = self.faked(turn(START), *[STOP] * STOP_LIMIT)
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))
        self.assertIn(f"delegate: no outcome after 3 turn ends; {LEFT_OPEN}", self.err)
        self.assertEqual(self.kinds(), ["input", "progress", "nudge", "give_up", "result", "end"])
        self.assertEqual(
            bare(self.last("give_up")),
            {"kind": "give_up", "reason": "no outcome after 3 turn ends"},
        )
        self.assertEqual(
            (self.last("end")["rc"], [type(e) for e in self.events]), (0, [Begun, Progress])
        )

    def test_a_mark_resets_the_count_before_its_turn_end_counts_and_never_the_nudge(self):
        mark = turn("[agent-pm-progress:round] half way")
        for name, steps in (("once", [mark]), ("repeated on the next turn", [mark, mark])):
            with self.subTest(name):
                self.fresh()
                r = self.faked(
                    turn(START),
                    *[STOP] * (STOP_LIMIT - 1),
                    *steps,
                    *[STOP] * (STOP_LIMIT - 2),
                    turn(block(DONE)),
                )
                self.assertEqual((r.outcome.status, self.handle.calls), ("done", [("send", NUDGE)]))
                self.assertEqual(self.err.count("Progress (round): half way"), len(steps))

    def test_turn_ends_with_work_pending_neither_nudge_nor_count_however_many(self):
        r = self.faked(*[PENDING] * (2 + STOP_LIMIT), turn(START, block(DONE)))
        self.assertEqual((r.outcome.status, self.handle.calls), ("done", []))
        r = self.faked(PENDING, PENDING, STOP, *[PENDING, STOP] * STOP_LIMIT)
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))

    def test_an_invalid_block_is_fixed_by_one_send_that_is_neither_the_nudge_nor_counted(self):
        fix = ("send", FIX.format(error=INVALID_ERROR))
        for later, sends in (([], [fix]), ([STOP] * STOP_LIMIT, [fix, ("send", NUDGE)])):
            with self.subTest(later=len(later)):
                self.fresh()
                r = self.faked(turn(START, block(INVALID)), *later, turn(block(DONE)))
                self.assertEqual(
                    (r.returncode, r.outcome.status, self.handle.calls), (0, "done", sends)
                )
                self.assertEqual(
                    (self.started.call_count, self.kinds()[:3]),
                    (1, ["input", "progress", "invalid"]),
                )
                self.assertIn(
                    f"delegate: invalid outcome: {INVALID_ERROR}; resuming once\n", self.err
                )

    def test_an_invalid_block_twice_or_a_failed_fix_send_is_the_error(self):
        r = self.faked(turn(START, block(INVALID)), turn(block(INVALID)))
        self.assertEqual(r, Result(0, None, f"invalid outcome: {INVALID_ERROR}"))
        self.assertEqual(self.handle.calls, [("send", FIX.format(error=INVALID_ERROR))])
        r = self.faked(turn(START, block(INVALID)), fail=DriverError("gone"))
        self.assertEqual(
            r, Result(1, None, f"invalid outcome: {INVALID_ERROR}; resume failed: gone")
        )

    def test_an_idle_timeout_gives_up_and_leaves_the_session(self):
        r = self.faked(turn(START), IdleTimeout(IDLE_TIMEOUT))
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))
        self.assertIn(f"delegate: no event for 2 h; {LEFT_OPEN}", self.err)
        self.assertEqual(
            self.kinds(), ["input", "progress", "nudge", "idle", "give_up", "result", "end"]
        )
        self.assertEqual(
            (self.last("idle")["seconds"], self.last("give_up")["reason"]),
            (IDLE_TIMEOUT, "no event for 2 h"),
        )

    def test_hours_of_activity_or_blocked_without_an_idle_timeout_never_give_up(self):
        now = [0.0]

        def hours_apart(*events):
            for event in events:
                now[0] += 3 * 60 * 60
                yield event

        handle = FakeHandle(
            hours_apart(
                Activity("Bash"),
                Blocked("permission"),
                Activity(""),
                Blocked("question"),
                turn(START, block(DONE)),
            )
        )
        with (
            mock.patch("time.monotonic", lambda: now[0]),
            mock.patch.object(self.driver, "start", side_effect=handle.start),
        ):
            r = self.go(mode="tui")
        self.assertEqual((r.outcome.status, handle.calls, now[0]), ("done", [], 15 * 60 * 60))
        self.assertEqual(self.kinds(), ["input", "progress", "result", "end"])

    def test_a_failed_nudge_is_printed_and_counts_as_the_nudge(self):
        r = self.faked(*[STOP] * (1 + STOP_LIMIT), fail=DriverError("no pane"))
        self.assertEqual((r, self.handle.calls), (GAVE_UP, [("send", NUDGE)]))
        self.assertIn("delegate: tui: no pane\n", self.err)
        self.assertIn("nudge", self.kinds())

    def test_a_usage_limit_stops_the_session_without_a_nudge(self):
        at = 1767225600.0
        r = self.faked(LimitHit(at), STOP, Exited(-signal.SIGHUP))
        when = datetime.fromtimestamp(at).astimezone().isoformat(timespec="seconds")
        self.assertEqual(
            r, Result(-signal.SIGHUP, None, f"usage limit; resets at {when}", Limit(at))
        )
        self.assertEqual(
            (self.handle.calls, self.kinds()), ([("stop",)], ["input", "limit", "result", "end"])
        )

    def test_a_session_exiting_without_an_outcome_is_the_error(self):
        for events, result in (
            ([STOP, Exited(3)], Result(3, None, "the client exited 3")),
            ([Exited(0)], GAVE_UP),
        ):
            with self.subTest(events=events):
                self.fresh()
                self.assertEqual(self.faked(*events), result)
                self.assertNotIn(("stop",), self.handle.calls)

    def test_an_on_event_raise_stops_the_session_and_propagates(self):
        failure = RuntimeError("boom")

        def boom(event):
            if isinstance(event, Progress):
                raise failure

        with self.assertRaises(RuntimeError) as caught:
            self.faked(turn(START), on_event=boom)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.handle.calls, [("stop",)])
        self.assertEqual(self.last("result")["error"], "stopped: RuntimeError: boom")

    def test_a_failed_stop_is_printed_and_the_runs_exception_raised(self):
        for error in (RuntimeError("boom"), KeyboardInterrupt()):

            def raise_(event, error=error):
                if isinstance(event, Progress):
                    raise error

            with self.subTest(error=error):
                self.fresh()
                with self.assertRaises(type(error)) as cm:
                    self.faked(turn(START), on_event=raise_, stop_fail=DriverError("gone"))
                self.assertIs(cm.exception, error)
                self.assertEqual(
                    (self.handle.calls, self.err.count("delegate: tui: gone\n")), ([("stop",)], 1)
                )
                self.assertTrue(
                    self.last("result")["error"].startswith(f"stopped: {type(error).__name__}")
                )

    def test_a_failed_stop_is_printed_and_never_replaces_the_limit_or_stale_out_result(self):
        gone = DriverError("gone")
        r = self.faked(LimitHit(None), stop_fail=gone)
        self.assertEqual(r, Result(0, None, "usage limit; reset time unknown", Limit(None)))
        self.assertEqual(
            (self.handle.calls, self.err.count("delegate: tui: gone\n")), ([("stop",)], 1)
        )
        self.assertEqual(self.kinds(), ["input", "limit", "result", "end"])
        blocker = os.path.join(self.tmp, "file")
        self.write(blocker, "")
        self.fresh()
        r = self.faked(
            turn(START, block(DONE)), stop_fail=gone, out=os.path.join(blocker, "out.md")
        )
        self.assertEqual(
            (r.returncode, r.outcome, self.events, self.handle.calls), (1, None, [], [("stop",)])
        )
        self.assertRegex(r.error, r"^cannot remove the stale out: ")
        self.assertEqual(self.err.count("delegate: tui: gone\n"), 1)

    def test_a_driver_failure_at_start_is_the_result_and_a_runtime_without_tui_a_config_error(self):
        os.makedirs(self.work)
        with mock.patch.object(self.driver, "start", side_effect=DriverError("no tmux")):
            self.assertEqual(self.go(mode="tui"), Result(1, None, "tui: no tmux"))
        self.fresh()
        with self.assertRaises(ConfigError) as cm:
            self.go(mode="tui")
        self.assertEqual(
            (str(cm.exception), os.path.lexists(self.work)),
            ("runtime 'fake' does not support tui mode", False),
        )

    def test_headless_turn_ends_never_nudge_and_an_idle_timeout_is_only_recorded(self):
        r = self.faked(
            *[STOP] * (2 + STOP_LIMIT),
            IdleTimeout(IDLE_TIMEOUT),
            turn(START, block(DONE)),
            Exited(0),
            mode="headless",
        )
        self.assertEqual((r.outcome.status, self.handle.calls), ("done", []))
        self.assertEqual(self.kinds(), ["input", "idle", "progress", "result", "end"])
        args, given = self.started.call_args
        self.assertEqual(
            (given, args[0].session.sid, args[0].session.resume, args[0].session.events),
            ({"idle_timeout": IDLE_TIMEOUT}, self.events[0].sid, False, None),
        )


class InstructionRuns(InstructionCase):
    def test_each_role_gets_its_planned_prompt_without_an_outcome_or_events(self):
        for role in ROLES:
            with self.subTest(role=role):
                args = self.given(role)
                want, events = plan(*args, driver=self.driver).prompt, []
                with no_child(), redirect_stderr(io.StringIO()):
                    r = run(*args, on_event=events.append, driver=self.driver)
                self.assertEqual((r.returncode, r.outcome, r.instruction), (0, None, args[1].out))
                with open(args[1].out) as f:
                    self.assertEqual(f.read(), want)
                self.assertEqual(marks(want), [("round", "a mark line in the input")])
                self.assertEqual(events, [])
                self.assertFalse(os.path.exists(os.path.join(args[1].workdir, "run.jsonl")))

    def test_an_unconfigured_runtime_does_not_start_a_session(self):
        args = self.given("dummy-tester", runtime="unconfigured")
        with no_child():
            result = run(*args, driver=self.driver)
        self.assertEqual(
            (result.returncode, result.outcome, result.instruction), (0, None, args[1].out)
        )

    def test_a_resume_is_refused(self):
        args = self.given("dummy-tester", resume=True)
        with (
            no_child(),
            self.assertRaisesRegex(ConfigError, "instruction has no session to resume"),
        ):
            run(*args, driver=self.driver)
