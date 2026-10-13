from agent_pm.linear_orchestrator.engine import router
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.board_ids import TASK_GROUP, TEAM
from orchestrator_test_helpers.unit.router.data import (
    LIGHT,
    ORPHAN_LABEL,
    PD,
    RECHECK,
    SESSIONS,
    STATES,
    blocker,
    issue,
    label,
    who,
    writes,
)
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear
from orchestrator_test_helpers.unit.router.task_labels_fixtures import TaskLabels
from orchestrator_test_helpers.unit.router.tick_task_labels_fixtures import (
    TickTaskLabels as TickTaskLabelsBase,
)


class TickTaskLabels1(TickTaskLabelsBase):
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
                self.assertEqual((t["state"], t["comments"]), ("In Review", [TaskLabels.ORPHAN]))
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
                "recover TASK-1 to=in_review reason=role (researcher) is not the assignee's (engineer)"
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
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.INTERRUPTED])
        self.assertIn("claim TASK-1 role=researcher task=light-research runtime=a", self.said())
        (launch,) = self.start.launches
        self.assertEqual((launch.task, launch.mode), ("light-research", "new"))
        self.assertEqual(self.last_run(), ("start", "TASK-1", launch.sid, "researcher", "a"))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")
