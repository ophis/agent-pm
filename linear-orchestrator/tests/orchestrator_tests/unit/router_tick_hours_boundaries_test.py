import json
import os

from agent_pm.linear_orchestrator.engine import config, router
from orchestrator_tests.unit.clock import frozen
from orchestrator_tests.unit.router_data import (
    CONFIG,
    CONFIG_ENG2,
    DR,
    FULL,
    IDS,
    PD,
    RECHECK,
    ROLE,
    SESSIONS,
    ago,
    blocker,
    issue,
    outer_args,
    reading,
)
from orchestrator_tests.unit.router_fakes import FakeLinear
from orchestrator_tests.unit.router_scenarios import TickScenario


class TickHoursBoundaries(TickScenario):
    def test_hours_boundaries(self):
        for hour, ran in ((0, False), (1, True), (6, True), (7, False), (12, False), (23, False)):
            with self.subTest(hour=hour):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, hour=hour), 0)
                self.assertEqual(len(self.start.launches), int(ran))
                self.assertEqual(self.said() == [], not ran)
                self.assertEqual(self.api.order == [], not ran)

    def test_launchd_path_replaced(self):
        self.tick(FakeLinear([]))
        self.assertEqual(self.path, config.path())
        self.assertIn("/opt/homebrew/bin", self.path)

    def test_now_skips_hours_and_starts(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, "--now", hour=12)
        (launch,) = self.start.launches
        self.assertEqual(
            launch, outer_args("TASK-1", IDS[DR], ROLE["researcher"], launch.sid, None, "new", "a")
        )
        self.assertRegex(launch.sid, config.UUID_RE)
        self.assertEqual(self.last_run(), ("start", "TASK-1", launch.sid, "researcher", "a"))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_begin_appends_one_json_line_to_runs_jsonl(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with frozen("2026-10-09T00:26:33-04:00"):
            self.tick(fake, "--now", hour=12)
        (launch,) = self.start.launches
        self.assertEqual(
            self.state,
            '{"ts": "2026-10-09T00:26:33-04:00", "kind": "start", "issue": "TASK-1", '
            f'"sid": "{launch.sid}", "role": "researcher", "runtime": "a"}}\n',
        )
        self.assertEqual(
            sorted(os.listdir(os.path.dirname(self.runs))),
            ["orchestrator.jsonl", "router.lock", "runs.jsonl"],
        )

    def test_an_idle_tick_writes_no_line(self):
        cases = [
            (12, [], [issue("TASK-1", "Todo", "researcher")]),
            (2, FULL, [issue("TASK-1", "Todo", "researcher")]),
            (2, ["agent-pm-engineer-TASK-2"], [issue("TASK-2", "In Progress", "engineer")]),
        ]
        for hour, live, listed in cases:
            with self.subTest(hour=hour, live=live):
                self.assertEqual(self.tick(FakeLinear(listed), hour=hour, live=live), 0)
                self.assertEqual((self.said(), self.err), ([], ""))
        self.assertFalse(os.path.exists(os.path.join(self.logs, "orchestrator.jsonl")))
        blocked = [issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")])]
        self.tick(FakeLinear(blocked))
        self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7"]'])
        self.tick(FakeLinear(blocked), again=True)
        self.assertEqual(self.said(), [])

    def test_unknown_agent_runs_change_nothing(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        before = self.before()
        for argv in ((), ("--dry-run",), ("--now",)):
            with self.subTest(argv=argv):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv, live=None), 0)
                self.assertEqual(self.said(), ["skip reason=agent runs unknown"])
                self.assertEqual(
                    (fake.queries, self.start.launches, self.api.usage_calls, self.state),
                    ([], [], [], before),
                )

    def test_all_full_skips_before_linear(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        for cfg, live in ((CONFIG, FULL), (CONFIG_ENG2, [*FULL, "agent-pm-engineer-TASK-9"])):
            self.write_config(cfg)
            counts = json.dumps(
                {
                    r: f"{n}/{n}"
                    for r, n in (("engineer", len(live) - 2), ("pm", 1), ("researcher", 1))
                }
            )
            for argv in ((), ("--dry-run",), ("--now",)):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv, live=live), 0)
                self.assertEqual(
                    self.said(),
                    [f"skip reason=all roles full live={counts}"] if "--dry-run" in argv else [],
                    (cfg, argv),
                )
                self.assertEqual(
                    (fake.queries, self.api.order, self.api.usage_calls), ([], SESSIONS, []), argv
                )
                self.assertIn("TASK-8", self.state, argv)

    def test_partly_full_role_gets_a_launch(self):
        self.write_config(CONFIG_ENG2)
        fake = self.fake(
            issue("TASK-1", "In Progress", "engineer", priority=1, project=PD),
            issue("TASK-2", "Todo", "engineer", priority=2, project=PD),
            issue("TASK-3", "Todo", "engineer", priority=3, project=PD),
        )
        self.resumable("TASK-1", "a", 60)
        self.tick(fake, live=["agent-pm-engineer-TASK-1"])
        self.assertEqual(
            self.said(),
            [
                'plan mode=new queue=2 live={"engineer": "1/2"}',
                "pick TASK-2 queue=2",
                "claim TASK-2 role=engineer runtime=a",
                "launch TASK-2 exit=0",
            ],
        )
        self.assertEqual(self.launched_runs(), [("TASK-2", "new")])
        self.assertEqual(
            {i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
            {
                "TASK-1": ("In Progress", None),
                "TASK-2": ("In Progress", None),
                "TASK-3": ("Todo", None),
            },
        )

    def test_todo_issue_with_live_session_not_claimed(self):
        self.write_config(CONFIG_ENG2)
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "engineer", priority=1, project=PD),
                issue("TASK-2", "Todo", "engineer", priority=2, project=PD),
            ]
        )
        self.tick(fake, live=["agent-pm-engineer-TASK-1"])
        self.assertEqual(
            self.said(),
            [
                'plan mode=new queue=1 live={"engineer": "1/2"}',
                "pick TASK-2 queue=1",
                "claim TASK-2 role=engineer runtime=a",
                "launch TASK-2 exit=0",
            ],
        )
        self.assertEqual(
            {i: t["state"] for i, t in fake.issues.items()},
            {"TASK-1": "Todo", "TASK-2": "In Progress"},
        )

    def test_full_role_todo_not_claimed_and_non_live_in_progress_recovered(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "engineer", priority=1, updated=ago(hours=3)),
            issue("TASK-2", "In Progress", "engineer", updated=ago(hours=3)),
            issue("TASK-3", "In Progress", "engineer"),
            issue("TASK-4", "Todo", "engineer", priority=1),
            issue("TASK-5", "Todo", "pm", priority=3, project=PD),
            issue("TASK-6", "Todo", "engineer", priority=1),
        )
        for ident in ("TASK-1", "TASK-3", "TASK-4"):
            for m in (390, 380, 370, 360):
                self.add("start", ident, f"{ident}-{m}", m)
        self.tick(fake, live=["agent-pm-engineer-TASK-1"])
        self.assertEqual(
            self.said(),
            [
                "recover TASK-3 to=in_review reason=reached 4 attempts",
                f"recover TASK-2 to=todo reason=no session updated={ago(hours=3)}",
                'plan mode=new queue=1 live={"engineer": "1/1"}',
                "pick TASK-5 queue=1",
                "claim TASK-5 role=pm runtime=b",
                "launch TASK-5 exit=0",
            ],
        )
        self.assertEqual(
            {i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
            {
                "TASK-1": ("In Progress", None),
                "TASK-2": ("Todo", [router.INTERRUPTED]),
                "TASK-3": ("In Review", [router.CAP_COMMENT]),
                "TASK-4": ("Todo", None),
                "TASK-5": ("In Progress", None),
                "TASK-6": ("Todo", None),
            },
        )
        self.assertEqual(self.launched(), "TASK-5")
        self.assertNotIn("TASK-1", [v["i"] for q, v in fake.queries if "history" in q])
        flts = [v["f"] for q, v in fake.queries if "issues(filter" in q]
        self.assertEqual(
            {tuple(sorted(f["assignee"]["id"]["in"])) for f in flts},
            {tuple(sorted(f"u-{r}" for r in ROLE))},
        )

    def test_full_role_resume_not_launched(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "pm", priority=1, project=PD),
            issue("TASK-2", "In Progress", "researcher", priority=3),
            issue("TASK-3", "Todo", "engineer", priority=1),
        )
        self.resumable("TASK-1", "a", 120, "pm")
        self.resumable("TASK-2", "b", 60)
        self.tick(fake, live=["agent-pm-pm-TASK-9"])
        self.assertEqual(self.said()[0], 'plan TASK-2 mode=resume sid=b live={"pm": "1/1"}')
        self.assertEqual(self.launched_runs(), [("TASK-2", "resume")])
        self.assertEqual(
            {i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
            {
                "TASK-1": ("In Progress", None),
                "TASK-2": ("In Progress", None),
                "TASK-3": ("Todo", None),
            },
        )
        self.assertEqual(self.last_run(), ("resume", "TASK-2", "b", "researcher", "a"))
        self.assertEqual(self.api.usage_calls, ["a"])

    def test_nothing_to_do_with_full_role(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "engineer"),
                issue("TASK-2", "In Progress", "engineer", updated=ago(hours=3)),
            ]
        )
        self.tick(fake, live=["agent-pm-engineer-TASK-2"])
        self.assertEqual(self.said(), [])
        self.assertEqual((self.api.order, self.api.usage_calls), (SESSIONS, []))
        self.assertEqual(
            [v["e"] for q, v in fake.queries if "users(filter" in q],
            [ROLE["researcher"], ROLE["pm"], ROLE["engineer"]],
        )
        self.assertEqual(fake.mutations, [])

    def test_one_run_per_tick_resume_first(self):
        self.write_config(CONFIG_ENG2)
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=3),
            issue("TASK-8", "In Progress", "engineer", priority=4, project=PD),
            issue("TASK-2", "Todo", "researcher", priority=1),
            issue("TASK-3", "Todo", "pm", priority=2, project=PD),
            issue("TASK-4", "Todo", "engineer", priority=2, project=PD),
        )
        self.resumable("TASK-1", "a", 60)
        self.resumable("TASK-8", "h", 70, "engineer")
        self.tick(fake)
        self.assertEqual(self.said(), ["plan TASK-1 mode=resume sid=a", "launch TASK-1 exit=0"])
        self.assertEqual(self.launched_runs(), [("TASK-1", "resume")])
        self.assertEqual(
            {i: t["state"] for i, t in fake.issues.items()},
            {
                "TASK-1": "In Progress",
                "TASK-8": "In Progress",
                "TASK-2": "Todo",
                "TASK-3": "Todo",
                "TASK-4": "Todo",
            },
        )
        self.assertEqual((self.api.usage_calls, self.api.order), (["a"], SESSIONS))

    def test_one_claim_per_tick(self):
        self.write_config(CONFIG_ENG2)
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "pm", priority=2, project=PD),
                issue("TASK-3", "Todo", "engineer", priority=2, project=PD),
            ]
        )
        self.tick(fake)
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=3",
                "pick TASK-1 queue=3",
                "claim TASK-1 role=researcher runtime=a",
                "launch TASK-1 exit=0",
            ],
        )
        self.assertEqual((self.api.order, self.api.usage_calls), (SESSIONS, ["a"]))
        self.assertEqual(
            {i: t["state"] for i, t in fake.issues.items()},
            {"TASK-1": "In Progress", "TASK-2": "Todo", "TASK-3": "Todo"},
        )

    def test_dry_run_plans_one_run(self):
        self.write_config(CONFIG_ENG2)
        fake = self.fake(
            issue("TASK-3", "Todo", "pm", priority=1, project=PD),
            issue("TASK-4", "Todo", "pm", priority=3, project=PD),
            issue("TASK-5", "Todo", "engineer", priority=2, project=PD),
            issue("TASK-6", "Todo", "engineer", priority=3, project=PD),
        )
        for sid in "wxyz":
            self.add("start", "TASK-3", sid, 300)
        before = self.before()
        self.assertEqual(self.tick(fake, "--dry-run"), 0)
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=4",
                "claim-skip TASK-3 to=in_review reason=reached 4 attempts",
                "pick TASK-5 queue=4",
                "usage runtime=a usage=five_hour=10% weekly=10% allowed=true",
            ],
        )
        self.assertEqual((self.api.order, self.api.usage_calls), (SESSIONS, ["a"]))
        self.assertEqual((fake.mutations, self.state, self.start.launches), ([], before, []))
        self.assertNotIn(RECHECK, [q for q, _ in fake.queries])

    def test_dry_run_usage_blocked(self):
        self.api.usages = {"a": reading(95)}
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.tick(fake, "--dry-run"), 0)
        why = "usage=five_hour=95% weekly=10%"
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=1",
                "pick TASK-1 queue=1",
                f"usage runtime=a {why} allowed=false",
                f"usage-skip TASK-1 runtime=a reason=usage {why}",
                "pick-none reason=nothing claimable",
                "skip reason=nothing claimed",
            ],
        )
        self.assertEqual((fake.mutations, self.start.launches), ([], []))

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

