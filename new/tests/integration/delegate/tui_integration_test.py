"""delegate.run in tui mode on a private tmux server, set up as the Driver's tui_integration_test, the core config's
ClaudeAdapter running new/tests/fake_claude.py interactively (a Stop hook step: a turn end with no prompt before it).
The tui policy end to end (FR-21, FR-23, FR-24): the nudge, the give-up, pending turn ends, a mark's reset, the idle
give-up on a clock ahead (driver.api's `time` only), the fix send; the session left open. Main's
tests/integration/core/drive_tui_integration_test.py, its tui run."""

from new.tests import fake_claude
from new.tests.integration.delegate.cli_integration_test import (
    DONE,
    SID,
    UNSAVED,
    UNSAVED_ERROR,
    mark,
    prompt,
)
from new.tests.integration.driver import tui_integration_test as tui_live
from new.tests.unit.delegate.runner_test import GAVE_UP, block

import io
import json
import os
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

from agent_pm import composer
from agent_pm.delegate import Progress, run
from agent_pm.delegate.runner import FIX, IDLE_TIMEOUT, NUDGE, STOP_LIMIT
from agent_pm.driver import api, registry

NAME = f"claude-{SID[:8]}"
LEFT_OPEN = f"session {NAME} left open: tmux attach -t '={NAME}'\n"
WAIT = 60  # seconds: a run
START = mark("start", "echoing")
TURN_1 = ["session", "input", "client_session", "progress"]


def stop(text: str, pending: int = 0) -> dict:
    return {
        "kind": "hook",
        "event": "Stop",
        "last_assistant_message": text,
        "background_tasks": [{}] * pending,
    }


class Ahead:
    """driver.api's `time`: monotonic() `by` s ahead of the real clock."""

    def __init__(self):
        self.by = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.by


class TuiRun(tui_live.Live):
    """dummy-tester's echo as session SID in workdir `work`, out `out.md`, events file self.events."""

    def setUp(self):
        super().setUp()
        self.scenario, self.log = (
            os.path.join(self.dir, "claude.json"),
            os.path.join(self.dir, "claude.log"),
        )
        self.out = os.path.join(self.workdir, "out.md")
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "config"),
                    fake_claude.ENV: self.scenario,
                },
            )
        )
        registry.reset()

    def go(self, on_event=None, **scenario):
        """run() with the fake playing `scenario`, failing past WAIT s; self.seen gets each callback, self.err the
        stderr."""
        with open(self.scenario, "w") as f:
            json.dump({**scenario, "log": self.log}, f)
        config, self.seen, err, ended = composer.resolve("dummy-tester"), [], io.StringIO(), []

        def on(event):
            self.seen.append(event)
            if on_event:
                on_event(event)

        def target():
            try:
                ended.append(
                    run(
                        config,
                        "echo",
                        "Hello.",
                        self.workdir,
                        runtime="claude",
                        mode="tui",
                        sid=SID,
                        events=self.events,
                        out=self.out,
                        cwd=self.workdir,
                        on_event=on,
                    )
                )
            except BaseException as e:
                ended.append(e)

        thread = threading.Thread(target=target, daemon=True)
        with redirect_stderr(err):
            thread.start()
            thread.join(WAIT)
        self.err = err.getvalue()
        if not ended:
            self.fail(f"no result within {WAIT} s; stderr:\n{self.err}")
        if isinstance(ended[0], BaseException):
            raise ended[0]
        return ended[0]

    def records(self) -> list[dict]:
        with open(os.path.join(self.workdir, "run.jsonl")) as f:
            return [json.loads(line) for line in f]

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.records()]

    def last(self, kind: str) -> dict:
        return [e for e in self.records() if e["kind"] == kind][-1]

    def users(self) -> list[str]:
        """The session's prompts: its first, then each text sent to it."""
        with open(self.last("session")["transcript"]) as f:
            found = [r["message"]["content"] for r in map(json.loads, f) if r["type"] == "user"]
        with open(self.log) as f:
            (call,) = map(json.loads, f)
        self.assertEqual(found[0], prompt(call))
        return found[1:]

    def left_open(self) -> None:
        shown = self.server.tmux(
            "display-message", "-p", "-t", f"={NAME}:", "#{session_name} #{pane_dead}"
        )
        self.assertEqual(shown.stdout, f"{NAME} 0\n", shown.stderr)


class TurnEnds(TuiRun):
    def test_the_nudges_turn_ends_done_the_deliverable_saved_the_session_left_open(self):
        result = self.go(steps=[START], turns=[[block(DONE)]])
        self.assertEqual(
            (result.returncode, result.outcome.status, result.error), (0, "done", ""), self.err
        )
        self.assertIn(f"delegate: tui session {NAME}: tmux attach -t '={NAME}'\n", self.err)
        self.assertEqual(self.users(), [NUDGE])
        self.assertEqual(self.kinds(), [*TURN_1, "nudge", "result", "end"])
        with open(self.out) as f:
            self.assertEqual(f.read(), DONE["deliverable"])
        self.left_open()
        self.lines(f"{NAME} done", f"{NAME} done")

    def test_after_the_nudge_stop_limit_turn_ends_without_an_outcome_give_up_the_session_left_open(
        self,
    ):
        result = self.go(
            steps=[START], turns=[[*[stop("Working.")] * (STOP_LIMIT - 1), "Working."]]
        )
        self.assertEqual(result, GAVE_UP)
        self.assertIn(f"delegate: no outcome after {STOP_LIMIT} turn ends; {LEFT_OPEN}", self.err)
        self.assertEqual(self.users(), [NUDGE])
        self.assertEqual(self.kinds(), [*TURN_1, "nudge", "give_up", "result", "end"])
        self.assertEqual(self.last("give_up")["reason"], f"no outcome after {STOP_LIMIT} turn ends")
        self.left_open()

    def test_turn_ends_with_work_pending_neither_nudge_nor_count(self):
        waiting = stop("Waiting for the build.", pending=1)
        result = self.go(
            steps=[waiting, waiting, START],
            turns=[[*[waiting, stop("Working.")] * (STOP_LIMIT - 1), waiting, block(DONE)]],
        )
        self.assertEqual((result.outcome.status, self.users()), ("done", [NUDGE]), self.err)

    def test_a_progress_mark_resets_the_count(self):
        half = mark("round", "half way")
        working = [stop("Working.")] * (STOP_LIMIT - 1)
        result = self.go(steps=[START], turns=[[*working, stop(half), *working[1:], block(DONE)]])
        self.assertEqual((result.outcome.status, self.users()), ("done", [NUDGE]), self.err)
        self.assertIn(Progress("round", "half way"), self.seen)


class Idle(TuiRun):
    def test_no_event_for_the_idle_timeout_gives_up_the_session_left_open(self):
        clock = Ahead()
        self.enterContext(mock.patch.object(api, "time", clock))

        def later(event):
            if isinstance(event, Progress):  # read at turn 1's end, the last event
                clock.by += IDLE_TIMEOUT + 1

        result = self.go(later, steps=[START], background=1)
        self.assertEqual(result, GAVE_UP)
        self.assertIn(f"delegate: no event for 2 h; {LEFT_OPEN}", self.err)
        self.assertEqual(self.kinds(), [*TURN_1, "idle", "give_up", "result", "end"])
        self.assertEqual(
            (self.last("idle")["seconds"], self.last("give_up")["reason"]),
            (IDLE_TIMEOUT, "no event for 2 h"),
        )
        self.assertEqual(self.users(), [])
        self.left_open()


class Fix(TuiRun):
    def test_an_invalid_outcome_is_fixed_by_one_send_to_the_session(self):
        result = self.go(steps=[f"{START}\n{block(UNSAVED)}"], turns=[[block(DONE)]])
        self.assertEqual((result.returncode, result.outcome.status), (0, "done"), self.err)
        self.assertIn(f"delegate: invalid outcome: {UNSAVED_ERROR}; resuming once\n", self.err)
        self.assertEqual(self.users(), [FIX.format(error=UNSAVED_ERROR)])
        self.assertEqual(self.kinds(), [*TURN_1, "invalid", "result", "end"])
        self.left_open()


if __name__ == "__main__":
    unittest.main()
