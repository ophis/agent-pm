from agent_pm.linear_orchestrator.engine.routing import types as routing
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_tests.unit.board_ids import TASK_GROUP, TEAM
from orchestrator_tests.unit.router.data import (
    CONFIG,
    DEEP,
    HUMANS,
    LIGHT,
    ORPHAN_LABEL,
    PD,
    RECHECK,
    SESSIONS,
    STATES,
    STRAY,
    TASK_LABELS,
    blocker,
    issue,
    label,
    who,
    writes,
)
from orchestrator_tests.unit.router.fakes import FakeLinear
from orchestrator_tests.unit.router.fixtures import TaskLabelsScenario, TickBase


class TaskSelection(TaskLabelsScenario):
    def test_task_for(self):
        outside = [
            label("Urgent", STRAY, None),
            label("Light Research", LIGHT, "00000000-0000-4000-8000-000000000003"),
        ]
        role_tasks = ["deep-research", "light-research"]
        label_tasks = {
            LIGHT: "light-research",
            DEEP: "deep-research",
            ORPHAN_LABEL: "product-design",
        }
        fix = "Fix the label or the assignee, then move the issue back to Todo."
        absent = (
            "is not in ~/.agent-pm/config.local.toml's [task_labels]. Fix the "
            "label, then move the issue back to Todo."
        )
        cases = (
            ([], (None, None)),
            (outside, (None, None)),
            ([label("Light Research", LIGHT)], ("light-research", None)),
            ([label("Renamed in Linear", LIGHT)], ("light-research", None)),
            (outside + [label("Light Research", LIGHT)], ("light-research", None)),
            ([label("Whatever", DEEP)], ("deep-research", None)),
            ([label("Light Research", STRAY)], (None, f'Task label "Light Research" {absent}')),
            ([label("light-research", STRAY)], (None, f'Task label "light-research" {absent}')),
            (
                [label("Orphan", ORPHAN_LABEL)],
                (
                    None,
                    (
                        f"""Task label "Orphan" is not one of researcher's tasks (deep-research, """
                        f"light-research). {fix}"
                    ),
                ),
            ),
            (
                [label("Light Research", LIGHT), outside[0], label("Deep Research", DEEP)],
                (
                    None,
                    (
                        "Several task labels (Light Research, Deep Research); keep one, then "
                        "move the issue back to Todo."
                    ),
                ),
            ),
        )
        for labels, want in cases:
            self.assertEqual(
                routing.task_for(labels, TASK_GROUP, "researcher", role_tasks, label_tasks),
                want,
                labels,
            )

    def test_fr7_invalid_label_goes_to_review(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    priority=1,
                    labels=[label("Orphan", ORPHAN_LABEL)],
                ),
                issue("TASK-2", "Todo", "researcher", priority=2),
            ]
        )
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        t = fake.issues["TASK-1"]
        self.assertEqual(
            (t["state"], t["assignee"], t["comments"], t["subscribers"]),
            ("In Review", who("researcher"), [self.ORPHAN], ["me@x.com"]),
        )
        self.assertEqual(
            writes(fake),
            [
                ("issueSubscribe", "TASK-1"),
                ("read", "TASK-1"),
                ("issueUpdate", "TASK-1"),
                ("commentCreate", "TASK-1"),
                ("issueUpdate", "TASK-2"),
            ],
        )
        self.assertEqual(fake.mutations[1][1]["s"], STATES["In Review"])
        self.assertIn("claim-skip TASK-1 to=in_review reason=bad task label", self.said())

    def test_capped_and_bad_label_todo_skipped_for_the_next(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue(
                    "TASK-2",
                    "Todo",
                    "researcher",
                    priority=2,
                    labels=[label("Orphan", ORPHAN_LABEL)],
                ),
                issue("TASK-3", "Todo", "pm", priority=3, project=PD),
                issue("TASK-4", "Todo", "engineer", priority=4, project=PD),
            ]
        )
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), f"TASK-3 https://linear.app/x/TASK-3 {PD}")
        self.assertEqual(
            self.said(),
            [
                "claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                "pick TASK-2 queue=4",
                "claim-skip TASK-2 to=in_review reason=bad task label",
                "pick TASK-3 queue=4",
                "claim TASK-3 role=pm runtime=b",
            ],
        )
        self.assertEqual(
            {i: fake.issues[i].get("comments") for i in ("TASK-1", "TASK-2")},
            {"TASK-1": [routing.CAP_COMMENT], "TASK-2": [self.ORPHAN]},
        )
        self.assertEqual([i for op, i in writes(fake) if op == "read"], ["TASK-1", "TASK-2"])
        self.assertEqual([v["i"] for q, v in fake.queries if q == RECHECK], ["TASK-2", "TASK-3"])
        self.assertEqual(fake.issues["TASK-4"]["state"], "Todo")

    def test_blocked_invalid_label_waits_for_its_blocker(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    labels=[label("Orphan", ORPHAN_LABEL)],
                    inverse=[blocker("TASK-7")],
                )
            ]
        )
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(
            self.said(), ['blocked TASK-1 by=["TASK-7"]', "pick-none reason=queue empty"]
        )
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("Todo", []))
        self.assertNotIn(RECHECK, [q for q, _ in fake.queries])
        fake.issues["TASK-1"]["inverseRelations"]["nodes"][0]["issue"]["state"]["type"] = (
            "completed"
        )
        self.assertEqual(self.claim(fake), "")
        t = fake.issues["TASK-1"]
        self.assertEqual((t["state"], t["comments"]), ("In Review", [self.ORPHAN]))
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=1",
                "claim-skip TASK-1 to=in_review reason=bad task label",
                "pick-none reason=nothing claimable",
            ],
        )

    def test_fr4_label_read_from_recheck(self):
        class Relabel(FakeLinear):
            def __call__(self, query, **v):
                out = super().__call__(query, **v)
                if "issues(filter" in query:
                    self.issues["TASK-1"]["labels"] = [label("Light Research", LIGHT)]
                return out

        fake = Relabel([issue("TASK-1", "Todo", "researcher")])
        self.claim(fake)
        self.assertIn("claim TASK-1 role=researcher task=light-research runtime=a", self.said())
        self.assertEqual(self.taken[1:], ("light-research", "researcher", "a"))
        self.assertNotIn("labels", fake.reads()[0])

    def test_fr8_dry_run_resolves_nothing(self):
        for argv in (False, True):
            fake = FakeLinear(
                [issue("TASK-1", "Todo", "researcher", labels=[label("Orphan", ORPHAN_LABEL)])]
            )
            self.assertEqual(
                self.claim(fake, dry=True, recover=argv),
                "TASK-1 https://linear.app/x/TASK-1 Deep Research",
                argv,
            )
            self.assertEqual(self.said(), ["pick TASK-1 queue=1"], argv)
            self.assertEqual(
                ([q for q, _ in fake.queries if q == RECHECK], fake.mutations), ([], []), argv
            )
            self.assertEqual(self.taken[1:], (None, "researcher", "a"))

    def test_nfr1_claim_reads_one_query_per_issue(self):
        fake = FakeLinear(
            [issue("TASK-1", "Todo", "researcher", labels=[label("Light Research", LIGHT)])]
        )
        self.claim(fake)
        self.assertEqual(
            [(q, v) for q, v in fake.queries if "issue(id:" in q], [(RECHECK, {"i": "TASK-1"})]
        )
        todo = [q for q, _ in fake.queries].index(fake.reads()[0])
        self.assertEqual([q for q, _ in fake.queries[todo + 1 :]], [RECHECK, fake.mutations[0][0]])

    def test_fr22_every_resumable_issue_is_checked(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "In Progress", "researcher", priority=1),
                issue("TASK-2", "In Progress", "researcher", priority=2),
            ],
            self.hist,
        )
        self.resumable("TASK-1", "a", 60)
        self.resumable("TASK-2", "b", 60, "pm")
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )
        self.assertEqual(
            (fake.issues["TASK-1"]["state"], fake.issues["TASK-2"]["state"]),
            ("In Progress", "In Review"),
        )
        self.assertIn(
            "recover TASK-2 to=in_review reason=role (pm) is not the assignee's (researcher)",
            self.said(),
        )


class ScheduledTaskSelection(TickBase):
    def setUp(self):
        super().setUp()
        self.write_config(HUMANS + CONFIG + TASK_LABELS)

    def test_fr1_tick_checks_the_task_group_once_and_stops_on_a_bad_one(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake)
        self.assertEqual(
            [v for q, v in fake.queries if q == linear.Q_TASK_GROUP], [{"i": TASK_GROUP}]
        )
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        fake.group = {"isGroup": False}
        with self.assertRaises(LinearError) as cm:
            self.tick(fake)
        self.assertEqual(
            str(cm.exception),
            f"linear-orchestrator/config.toml: task_label_group {TASK_GROUP} is not a label group",
        )
        self.assertEqual(
            (fake.queries, fake.mutations),
            ([(linear.Q_TEAM, {"t": TEAM}), (linear.Q_TASK_GROUP, {"i": TASK_GROUP})], []),
        )
        self.assertEqual(
            (self.api.order, self.api.usage_calls, self.start.launches), (SESSIONS, [], [])
        )

    def test_blocked_invalid_label_waits_for_its_blocker(self):
        for argv, plan, end, rc in (
            ((), ["plan mode=new queue=1"], ["skip reason=nothing claimed"], 0),
            (("--issue", "TASK-1"), [], [], 1),
        ):
            with self.subTest(argv=argv):
                fake = FakeLinear(
                    [
                        issue(
                            "TASK-1",
                            "Todo",
                            "researcher",
                            labels=[label("Orphan", ORPHAN_LABEL)],
                            inverse=[blocker("TASK-7")],
                        )
                    ]
                )
                self.assertEqual(self.tick(fake, *argv), rc)
                self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7"]'])
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], fake.mutations, self.start.launches),
                    ("Todo", [], []),
                )
                self.assertNotIn(RECHECK, [q for q, _ in fake.queries])
                fake.issues["TASK-1"]["inverseRelations"]["nodes"][0]["issue"]["state"]["type"] = (
                    "completed"
                )
                self.assertEqual(self.tick(fake, *argv), rc)
                t = fake.issues["TASK-1"]
                self.assertEqual(
                    (t["state"], t["comments"]), ("In Review", [TaskLabelsScenario.ORPHAN])
                )
                self.assertEqual(
                    self.said(),
                    plan
                    + [
                        "pick TASK-1 queue=1",
                        "claim-skip TASK-1 to=in_review reason=bad task label",
                        "pick-none reason=nothing claimable",
                    ]
                    + end,
                )
                self.assertEqual((self.start.launches, self.state), ([], ""))

    def test_fr20_resume_passes_no_task(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", labels=[label("Light Research", LIGHT)])
        )
        self.moved("TASK-1", 61)
        self.add("resume", "TASK-1", "a", 60)
        self.touch("a", 40)
        self.tick(fake)
        (launch,) = self.start.launches
        self.assertEqual((launch.sid, launch.task, launch.mode), (self.sid("a"), None, "resume"))
        self.assertEqual(self.last_run(), ("resume", "TASK-1", "a", "researcher", "a"))
        self.assertIn("plan TASK-1 mode=resume sid=a", self.said())

    def test_fr22_a_logged_role_not_the_assignees_goes_to_review(self):
        fake = self.fake(issue("TASK-1", "In Progress", "engineer", project=PD))
        self.resumable("TASK-1", "a", 60)
        before = self.before()
        self.tick(fake)
        t = fake.issues["TASK-1"]
        self.assertEqual(
            (t["state"], t["assignee"], t["comments"], t["subscribers"]),
            (
                "In Review",
                who("engineer"),
                [
                    "The interrupted agent run's role (researcher) is not the "
                    "assignee's (engineer); needs a look."
                ],
                ["me@x.com"],
            ),
        )
        self.assertEqual(
            writes(fake),
            [
                ("issueSubscribe", "TASK-1"),
                ("read", "TASK-1"),
                ("issueUpdate", "TASK-1"),
                ("commentCreate", "TASK-1"),
            ],
        )
        self.assertEqual(fake.mutations[1][1]["s"], STATES["In Review"])
        self.assertEqual(
            self.said(),
            [
                (
                    "recover TASK-1 to=in_review reason=role (researcher) is not the "
                    "assignee's (engineer)"
                )
            ],
        )
        self.assertEqual((self.start.launches, self.state), ([], before))

    def test_fr16_requeued_issue_resolved_again_at_claim(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", labels=[label("Light Research", LIGHT)])
        )
        self.moved("TASK-1", 61)
        self.add("start", "TASK-1", "a", 60)
        self.tick(fake)
        self.assertEqual(fake.issues["TASK-1"]["comments"], [routing.INTERRUPTED])
        self.assertIn("claim TASK-1 role=researcher task=light-research runtime=a", self.said())
        (launch,) = self.start.launches
        self.assertEqual((launch.task, launch.mode), ("light-research", "new"))
        self.assertEqual(self.last_run(), ("start", "TASK-1", launch.sid, "researcher", "a"))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")
