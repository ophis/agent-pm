"""`agent-pm-linear writeback` as a subprocess, one DelegateEvent line on stdin per call, FakeLinear
over HTTP: the run's claim comes from its runs.jsonl start row, each step is written once however
often its event arrives.
"""

import json
import os
import re
import subprocess
import unittest

from agent_pm.linear_orchestrator.engine import ops, runs
from orchestrator_tests.case import PY
from orchestrator_tests.integration.flow_fixtures import (
    ACCOUNTS,
    HARNESS_EMAIL,
    KEYS,
    PROJECT,
    TIMEOUT,
    Flow,
)
from orchestrator_tests.unit.run_fixtures import ID, SESSION, SID, UUID

ENTRY = os.path.join(os.path.dirname(__file__), "entry.py")

PICK = "picked the task."


class Writeback(Flow):
    def setUp(self):
        super().setUp()
        self.rd = os.path.join(self.runs_dir, ID)
        self.fake.issue(
            "Session registry",
            identifier=ID,
            id=UUID,
            state="in_progress",
            assignee=self.ids[ACCOUNTS["researcher"]],
            project=PROJECT,
        )

    def claim(self, role="researcher", key=None):
        runs.note(
            os.path.join(self.logs, "runs.jsonl"),
            "start",
            issue=ID,
            sid=SID,
            role=role,
            client="claude",
            delegate_kind="harness",
            uuid=UUID,
            project=PROJECT["id"],
            target=None,
            key=key or KEYS[role],
        )

    def feed(self, kind, attempt=1, **fields):
        line = json.dumps({"v": 1, "kind": kind, "sid": SID, "attempt": attempt, **fields})
        return subprocess.run(
            [*PY, ENTRY, "writeback", "--issue", ID, "--workdir", self.rd],
            cwd=self.root,
            env=self.env(),
            input=line + "\n",
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )

    def ok(self, kind, attempt=1, **fields):
        res = self.feed(kind, attempt, **fields)
        self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)

    def stored(self):
        return [(self.email(c["user"]), c["id"]) for c in self.issue(ID)["comments"]]


class BegunAndProgress(Writeback):
    def test_the_session_comment_as_the_harness_and_marks_as_the_role_each_once(self):
        self.claim()
        for _ in range(2):
            self.ok("begun")
            self.ok("progress", name="start", text=PICK)
        [running] = [b for _, b in self.comments(ID) if b.startswith(f"Run {SID}")]
        self.assertRegex(running, rf"\ARun {SID} · running · #1 · \S+\Z")
        researcher = ACCOUNTS["researcher"]
        self.assertEqual(
            self.stored(),
            [(HARNESS_EMAIL, SESSION), (researcher, ops.client_id(SID, "start"))],
        )
        self.assertEqual(self.comments(ID)[-1], (researcher, f"Research started: {PICK}"))
        self.assertEqual(self.ledger(ID), {SID: ["start"]})
        self.assertEqual(self.problems(), [("sessions.M_UPDATE", HARNESS_EMAIL, "error")])

    def test_a_changed_role_account_does_not_move_a_running_claim(self):
        self.claim(key=KEYS["pm"])
        self.ok("progress", name="start", text=PICK)
        self.assertEqual(self.stored(), [(ACCOUNTS["pm"], ops.client_id(SID, "start"))])

    def test_no_start_row_exits_2_and_writes_nothing(self):
        res = self.feed("begun")
        self.assertEqual((res.returncode, res.stdout), (2, ""))
        self.assertRegex(res.stderr, rf"agent-pm-linear writeback: {re.escape(ID)}: no start row")
        self.assertEqual((self.requests(), self.ledger(ID)), ([], {}))


if __name__ == "__main__":
    unittest.main()
