from agent_pm.linear_orchestrator.engine import router
from orchestrator_test_helpers.unit.router.changed_fixtures import Changed
from orchestrator_test_helpers.unit.router.data import (
    CONFIG,
    HUMANS,
    IDS,
    LIGHT,
    PD,
    ROLE,
    TASK_LABELS,
    USER,
    issue,
    label,
    who,
)
from orchestrator_test_helpers.unit.router.recheck_fixtures import Recheck as RecheckBase


class Recheck1(RecheckBase):
    def test_reassigned_to_no_role_account_is_not_claimed(self):
        for assignee in ({"id": USER, "email": "me@x.com"}, who("someone"), None):
            with self.subTest(assignee=assignee):
                fake = Changed(
                    [
                        issue("TASK-1", "Todo", "researcher", priority=1),
                        issue("TASK-2", "Todo", "researcher", priority=2),
                    ],
                    {"TASK-1": {"assignee": assignee}},
                )
                self.assertEqual(
                    self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research"
                )
                self.assertEqual(
                    self.said(),
                    [
                        "pick TASK-1 queue=2",
                        "claim-skip TASK-1 reason=not assigned to a role",
                        "pick TASK-2 queue=2",
                        "claim TASK-2 role=researcher runtime=a",
                    ],
                )
                self.assertEqual([v["i"] for _, v in fake.mutations], ["TASK-2"])

    def test_reassigned_to_another_role_is_claimed_as_that_role(self):
        fake = Changed([issue("TASK-1", "Todo", "researcher")], {"TASK-1": {"assignee": who("pm")}})
        self.assertEqual(
            self.claim(fake, gate=True), "TASK-1 https://linear.app/x/TASK-1 Deep Research"
        )
        taken, task, role, runtime = self.taken
        self.assertEqual((taken["assignee"], task, role, runtime), (who("pm"), None, "pm", "b"))
        self.assertEqual(self.said(), ["pick TASK-1 queue=1", "claim TASK-1 role=pm runtime=b"])
        self.assertEqual(self.api.usage_calls, ["b"])
        a = router.begin("new", taken, "s1", role, task, runtime, self.runs)
        self.assertEqual((a.assignee, a.runtime), (ROLE["pm"], "b"))

    def test_the_task_label_is_checked_against_the_new_role(self):
        self.write_config(HUMANS + CONFIG + TASK_LABELS)
        fake = Changed(
            [issue("TASK-1", "Todo", "researcher", labels=[label("Light Research", LIGHT)])],
            {"TASK-1": {"assignee": who("pm")}},
        )
        self.assertEqual(self.claim(fake), "")
        t = fake.issues["TASK-1"]
        self.assertEqual(
            (t["state"], t["comments"]),
            (
                "In Review",
                [
                    'Task label "Light Research" is not one of pm\'s tasks (product-design). '
                    "Fix the label or the assignee, then move the issue back to Todo."
                ],
            ),
        )

    def test_a_full_new_role_is_skipped(self):
        fake = Changed([issue("TASK-1", "Todo", "researcher")], {"TASK-1": {"assignee": who("pm")}})
        self.assertEqual(self.claim(fake, full={"pm"}), "")
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=1",
                "claim-skip TASK-1 reason=pm is full",
                "pick-none reason=nothing claimable",
            ],
        )
        self.assertEqual((fake.mutations, fake.issues["TASK-1"]["state"]), ([], "Todo"))

    def test_a_changed_project_is_the_runs(self):
        fake = Changed(
            [issue("TASK-1", "Todo", "researcher")],
            {"TASK-1": {"project": {"id": IDS[PD], "name": PD}}},
        )
        self.assertEqual(self.claim(fake), f"TASK-1 https://linear.app/x/TASK-1 {PD}")
        a = router.begin("new", self.taken[0], "s1", "researcher", None, "a", self.runs)
        self.assertEqual(a.project, IDS[PD])

    def test_a_removed_project_is_skipped(self):
        fake = Changed([issue("TASK-1", "Todo", "researcher")], {"TASK-1": {"project": None}})
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=1",
                "claim-skip TASK-1 reason=no project",
                "pick-none reason=nothing claimable",
            ],
        )
        self.assertEqual((fake.mutations, fake.issues["TASK-1"]["state"]), ([], "Todo"))
