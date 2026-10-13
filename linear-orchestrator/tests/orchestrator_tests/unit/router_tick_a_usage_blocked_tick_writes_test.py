import json
import os
from datetime import timedelta
from unittest import mock

from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.session import SessionStatus
from orchestrator_tests.unit.clock import frozen
from orchestrator_tests.unit.outage import FAILURES, FIELDS, failing
from orchestrator_tests.unit.router_data import (
    CONFIG,
    EAST,
    NOW,
    OVERLAY,
    PD,
    RECHECK,
    ago,
    issue,
    reading,
)
from orchestrator_tests.unit.router_fakes import FakeLinear, two
from orchestrator_tests.unit.router_scenarios import Start, TickBase, TickScenario


class TickAUsageBlockedTickWrites(TickScenario):
    def test_a_usage_blocked_tick_writes_only_its_usage_skips_once_a_day(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "engineer", priority=2, project=PD),
            ]
        )
        for again, five, said in (
            (
                False,
                95,
                [
                    f"usage-skip TASK-{n} runtime=a reason=usage usage=five_hour=95% weekly=10%"
                    for n in (1, 2)
                ],
            ),
            (True, 97, []),
        ):
            with self.subTest(again=again):
                self.api.usages = {"a": reading(five)}
                self.assertEqual(self.tick(fake, again=again), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual((self.start.launches, fake.mutations, self.state), ([], [], ""))

    def test_a_usage_skipped_head_then_a_claim_keeps_every_line(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        self.tick(two())
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=2",
                "pick TASK-1 queue=2",
                "usage-skip TASK-1 runtime=a reason=usage usage=five_hour=95% weekly=10%",
                "pick TASK-2 queue=2",
                "claim TASK-2 role=pm runtime=b",
                "launch TASK-2 exit=0",
            ],
        )

    def test_usage_skips_after_a_claim_skip_end_the_tick_quietly(self):
        self.api.usages = {"a": reading(95)}
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "researcher", priority=2),
            ]
        )
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.tick(fake)
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=2",
                "claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                "usage-skip TASK-2 runtime=a reason=usage usage=five_hour=95% weekly=10%",
            ],
        )

    def test_an_outage_mid_walk_writes_the_waiting_lines_first(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        gql = failing(FAILURES["timeout"], gql=fake, ops={"issue"})
        self.assertEqual(self.tick(gql), 1)
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=1",
                "pick TASK-1 queue=1",
                f"linear-error op=issue {FIELDS['timeout']}",
            ],
        )
        self.assertEqual((gql.failed[0][0], fake.mutations), (RECHECK, []))

    def test_trim_runs_before_plan(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        self.tick(FakeLinear([]))
        self.assertNotIn("TASK-8", self.state)

    def test_trim_failure_continues(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        with mock.patch.object(os, "replace", side_effect=OSError("disk")):
            self.tick(fake)
        self.assertIn("skip reason=prune failed error=OSError: disk", self.said())
        self.assertEqual(len(self.start.launches), 1)

    def test_the_outers_exit_and_exceptions_never_break_the_tick(self):
        cases = [
            (Start(exits={"TASK-1": 3}), "launch TASK-1 exit=3"),
            (
                Start(raises={"TASK-1": LinearError("linear api error: boom")}),
                "launch-error TASK-1 error=LinearError: linear api error: boom",
            ),
            (
                Start(raises={"TASK-1": RuntimeError("x")}),
                "launch-error TASK-1 error=RuntimeError: x",
            ),
        ]
        for start, end in cases:
            with self.subTest(end=end):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, start=start), 0)
                self.assertEqual(self.said()[-1], end)
                (launch,) = self.start.launches
                self.assertEqual(
                    self.last_run(), ("start", "TASK-1", launch.sid, "researcher", "a")
                )

    def test_resume_reads_the_session_at_its_recorded_cwd(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.resumable("TASK-1", "a", 60)
        self.api.records[self.sid("a")] = {"cwd": self.tmp}
        self.tick(fake)
        self.assertEqual(self.launched_runs(), [("TASK-1", "resume")])
        self.assertEqual(self.api.info, [("a", self.sid("a"), self.tmp)])

    def test_driver_session_counts_toward_max_runs_and_a_tui_session_does_not(self):
        tui = config.tui_name("engineer", "TASK-1", self.sid("a"))
        for live, ran in (([tui, "agent-pm-engineer-TASK-9"], []), ([tui], [("TASK-1", "new")])):
            with self.subTest(live=live):
                self.tick(FakeLinear([issue("TASK-1", "Todo", "engineer")]), live=live)
                self.assertEqual(self.launched_runs(), ran)


class TickGate(TickBase):
    """FR-46, FR-48, FR-60 over a tick: each candidate gates on its runtime; a resume keeps its
    recorded runtime.
    """

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

