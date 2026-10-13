import os
from unittest import mock

from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.engine.types import LinearError
from orchestrator_test_helpers.unit.outage import FAILURES, FIELDS, failing
from orchestrator_test_helpers.unit.router.data import PD, RECHECK, blocker, issue, reading
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear, two
from orchestrator_test_helpers.unit.router.start_fixtures import Start
from orchestrator_test_helpers.unit.router.tick_fixtures import Tick as TickBase


class Tick2(TickBase):
    def test_dry_run(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        fake = FakeLinear(
            [issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "Todo", "researcher")]
        )
        self.tick(
            fake, "--dry-run", hour=12, live=["agent-pm-pm-TASK-5", "agent-pm-engineer-TASK-6"]
        )
        self.assertEqual(
            self.said(),
            [
                "skip reason=outside hours",
                'plan mode=new queue=1 live={"engineer": "1/1", "pm": "1/1"}',
                "pick TASK-2 queue=1",
                "usage runtime=a usage=five_hour=10% weekly=10% allowed=true",
            ],
        )
        self.assertEqual(self.api.usage_calls, ["a"])
        self.assertEqual((self.start.launches, fake.mutations), ([], []))
        self.assertIn("TASK-8", self.state)

    def test_all_todo_blocked_reads_no_usage(self):
        def issues():
            return [
                issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-2", "unstarted")]),
                issue("TASK-2", "Todo", "pm", project=PD, inverse=[blocker("TASK-1", "unstarted")]),
                issue("TASK-3", "Todo", "engineer", inverse=[blocker("TASK-9", "started")]),
            ]

        blocked = [
            'blocked TASK-1 by=["TASK-2"]',
            'blocked TASK-2 by=["TASK-1"]',
            'blocked TASK-3 by=["TASK-9"]',
        ]
        for argv in ((), ("--dry-run",)):
            with self.subTest(argv=argv):
                fake = FakeLinear(issues())
                self.assertEqual(self.tick(fake, *argv), 0)
                self.assertEqual(self.said(), blocked)
                self.assertEqual(
                    (self.api.usage_calls, self.start.launches, fake.mutations), ([], [], [])
                )
                self.assertEqual(len(fake.reads()), 1)

    def test_ready_tick_reads_todo_once(self):
        for argv in ((), ("--issue", "TASK-2")):
            fake = FakeLinear(
                [
                    issue("TASK-1", "Todo", "researcher", priority=1, inverse=[blocker("TASK-7")]),
                    issue("TASK-2", "Todo", "researcher", priority=2),
                    issue("TASK-3", "Todo", "pm", priority=3, project=PD),
                ]
            )
            self.assertEqual(self.tick(fake, *argv), 0, argv)
            self.assertEqual(self.launched_runs(), [("TASK-2", "new")], argv)
            self.assertEqual(len(fake.reads()), 1, argv)
            self.assertEqual("plan mode=new queue=2" in self.said(), not argv, argv)
            self.assertEqual(self.said().count('blocked TASK-1 by=["TASK-7"]'), 1, argv)

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
