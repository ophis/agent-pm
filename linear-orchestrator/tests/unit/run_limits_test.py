import json
import os
from unittest import mock

from agent_pm.delegate import Limit, Result
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit.board_ids import STATES
from orchestrator_test_helpers.unit.run.data import (
    REPAIR,
    RUNNING,
    RUNTIME,
    T,
    cid,
    created,
    ended,
    updated,
)
from orchestrator_test_helpers.unit.run.limits_fixtures import Limits as LimitsBase
from orchestrator_test_helpers.unit.run_fixtures import ID, KEY, SID, UUID, Gql


class Limits1(LimitsBase):
    def test_a_usage_limit_writes_back_and_notes_it_in_the_runs_log(self):
        for at, said in (
            (T, f"resets at {writeback.local_time(T)}."),
            (None, "reset time unknown."),
        ):
            with self.subTest(at=at):
                self.gql = Gql()
                ledger = os.path.join(self.rd, writeback.LEDGER)
                if os.path.exists(ledger):
                    os.remove(ledger)
                self.done(result=Result(-9, None, "usage limit", Limit(at)))
                n = len(self.said())
                self.assertEqual(self.main(), 0)
                self.assertEqual(
                    self.said()[n:],
                    [
                        f"run launch TASK-7 mode=new sid={SID}",
                        f"run end TASK-7 sid={SID} exit=-9",
                        f"writeback step TASK-7 sid={SID} step=limit",
                    ],
                )
                self.assertEqual(
                    self.gql.calls,
                    [
                        *created(RUNNING),
                        ("subscribe", KEY, {"i": UUID, "e": "me@x.com"}),
                        (
                            "comment",
                            KEY,
                            {"c": cid("limit"), "i": UUID, "b": f"Usage limit, {said}"},
                        ),
                        ("reread", KEY, {"i": UUID}),
                        ("state", KEY, {"i": UUID, "s": STATES["in_review"]}),
                        *updated(ended("limit", -9)),
                        REPAIR,
                    ],
                )
                with open(self.runs) as f:
                    last = json.loads(f.read().splitlines()[-1])
                self.assertEqual(
                    {k: v for k, v in last.items() if k != "ts"},
                    {
                        "kind": "limit",
                        "issue": ID,
                        "sid": SID,
                        "runtime": RUNTIME,
                        "resets_at": writeback.local_time(at),
                    },
                )

    def test_a_failed_runs_note_is_a_run_error_and_the_session_comment_still_ends(self):
        os.makedirs(self.runs)
        self.done(result=Result(-9, None, "usage limit", Limit(T)))
        self.assertEqual(self.main(), 0)
        self.assertEqual(
            self.said()[1:],
            [
                f"run end TASK-7 sid={SID} exit=-9",
                f"writeback step TASK-7 sid={SID} step=limit",
                f"run run-error TASK-7 error=IsADirectoryError: [Errno 21] Is a directory: '{self.runs}'",
            ],
        )
        self.assertEqual(self.gql.calls[-2:], [*updated(ended("limit", -9)), REPAIR])

    def test_any_runs_note_exception_is_a_run_error_and_the_session_comment_still_ends(self):
        self.done(result=Result(-9, None, "usage limit", Limit(T)))
        with mock.patch.object(os, "fsync", side_effect=OSError("disk")):
            self.assertEqual(self.main(), 0)
        self.assertEqual(self.said()[-1], "run run-error TASK-7 error=OSError: disk")
        self.assertEqual(self.gql.calls[-2:], [*updated(ended("limit", -9)), REPAIR])
