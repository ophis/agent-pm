"""Tui behavior."""

import os
import signal
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
from agent_pm.delegate.engine.runner import FIX, IDLE_TIMEOUT, NUDGE, STOP_LIMIT
from agent_pm.session import (
    Activity,
    Blocked,
    DriverError,
    Exited,
    IdleTimeout,
    LimitHit,
    StartOptions,
)

from delegate.tests.replies import bare, block
from delegate.tests.unit.engine.runner.case import (
    INVALID,
    INVALID_ERROR,
    LEFT_OPEN,
    PENDING,
    SID,
    Base,
)
from delegate.tests.unit.runs import (
    DONE,
    GAVE_UP,
    NAME,
    START,
    STOP,
    FakeHandle,
    turn,
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
                self.assertEqual((args[:2], r.outcome.status), (("fake", "tui"), "done"))
                self.assertEqual(
                    (given, args[3].sid, args[3].name),
                    (
                        {"options": StartOptions(idle_timeout=IDLE_TIMEOUT, **want[0])},
                        self.events[0].sid,
                        want[1],
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
        def boom(event):
            if isinstance(event, Progress):
                raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.faked(turn(START), on_event=boom)
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
            (given, args[3].sid),
            ({"options": StartOptions(idle_timeout=IDLE_TIMEOUT)}, self.events[0].sid),
        )
