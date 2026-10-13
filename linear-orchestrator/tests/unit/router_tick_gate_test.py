import json
import os
from datetime import timedelta

from agent_pm.session import SessionStatus
from orchestrator_test_helpers.unit.clock import frozen
from orchestrator_test_helpers.unit.router.data import (
    CONFIG,
    EAST,
    NOW,
    OVERLAY,
    PD,
    ago,
    issue,
    reading,
)
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear, two
from orchestrator_test_helpers.unit.router.tick_gate_fixtures import TickGate as TickGateBase


class TickGate1(TickGateBase):
    def test_a_resume_keeps_the_runtime_of_its_claim(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with frozen(ago(minutes=60)):
            self.tick(fake)
        (claimed,) = self.start.launches
        self.assertEqual(self.last_run(), ("start", "TASK-1", claimed.sid, "researcher", "a"))
        with open(self.runs) as f:
            self.lines = f.read().splitlines()
        self.api.statuses[claimed.sid] = SessionStatus(
            "yes", (NOW - timedelta(minutes=40)).timestamp(), ""
        )
        self.write_config(CONFIG.replace(OVERLAY, '[core]\nruntime = "b"\n'))
        self.tick(fake)
        (resumed,) = self.start.launches
        self.assertEqual((resumed.sid, resumed.mode, resumed.runtime), (claimed.sid, "resume", "a"))
        self.assertEqual(self.last_run(), ("resume", "TASK-1", claimed.sid, "researcher", "a"))
        self.assertEqual(
            (self.api.info, self.api.usage_calls),
            ([("a", claimed.sid, os.path.join(self.work, "work", "TASK-1"))], ["a"]),
        )

    def test_a_blocked_resume_candidate_lets_a_claim_start(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=1),
            issue("TASK-2", "Todo", "pm", priority=2, project=PD),
        )
        self.resumable("TASK-1", "a", 60)
        self.assertEqual(self.tick(fake), 0)
        self.assertEqual(
            self.said(),
            [
                "usage-skip TASK-1 runtime=a reason=usage usage=five_hour=95% weekly=10%",
                "plan mode=new queue=1",
                "pick TASK-2 queue=1",
                "claim TASK-2 role=pm runtime=b",
                "launch TASK-2 exit=0",
            ],
        )
        (launch,) = self.start.launches
        self.assertEqual((launch.issue, launch.runtime), ("TASK-2", "b"))
        self.assertEqual(self.last_run(), ("start", "TASK-2", launch.sid, "pm", "b"))
        self.assertEqual(self.api.usage_calls, ["a", "b"])
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_all_blocked_starts_nothing_and_probes_a_runtime_once(self):
        self.api.usages = {"a": reading(95)}
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=1),
            issue("TASK-3", "In Progress", "engineer", priority=2, project=PD),
            issue("TASK-2", "Todo", "researcher", priority=2),
        )
        self.resumable("TASK-1", "a", 60)
        self.resumable("TASK-3", "c", 50, "engineer")
        before = self.before()
        why = "runtime=a reason=usage usage=five_hour=95% weekly=10%"
        for again, said in ((False, [f"usage-skip TASK-{n} {why}" for n in (1, 3, 2)]), (True, [])):
            with self.subTest(again=again):
                self.assertEqual(self.tick(fake, again=again), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual(
                    (self.start.launches, fake.mutations, self.state, self.api.usage_calls),
                    ([], [], before, ["a"]),
                )

    def test_a_live_limit_row_lets_the_other_runtime_start(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        self.limit("a", at)
        self.tick(two())
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=2",
                "pick TASK-1 queue=2",
                f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                "pick TASK-2 queue=2",
                "claim TASK-2 role=pm runtime=b",
                "launch TASK-2 exit=0",
            ],
        )
        self.assertEqual((self.launched_runs(), self.api.usage_calls), ([("TASK-2", "new")], ["b"]))
        self.assertEqual(
            [json.loads(x)["kind"] for x in self.state.splitlines()], ["limit", "start"]
        )

    def test_a_dry_run_walks_the_same_and_changes_nothing(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        b = "usage runtime=b usage=five_hour=10% weekly=10% allowed=true"
        rows = (
            (
                True,
                [
                    f"usage runtime=a until={at} allowed=false",
                    f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                    "plan mode=new queue=1",
                    "pick TASK-2 queue=1",
                    b,
                ],
                ["b"],
            ),
            (
                False,
                [
                    "usage runtime=a usage=five_hour=10% weekly=10% allowed=true",
                    "plan TASK-1 mode=resume sid=a",
                ],
                ["a"],
            ),
        )
        for limited, said, calls in rows:
            with self.subTest(limited=limited):
                self.lines, self.hist = [], {}
                if limited:
                    self.limit("a", at)
                fake = self.fake(
                    issue("TASK-1", "In Progress", "researcher", priority=1),
                    issue("TASK-2", "Todo", "pm", priority=2, project=PD),
                )
                self.resumable("TASK-1", "a", 60)
                before = self.before()
                self.assertEqual(self.tick(fake, "--dry-run"), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual(
                    (self.start.launches, fake.mutations, self.state, self.api.usage_calls),
                    ([], [], before, calls),
                )
