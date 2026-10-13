from agent_pm.linear_orchestrator.engine import promote
from orchestrator_test_helpers.unit.promote.test_failures_fixtures import (
    TestFailures as TestFailuresBase,
)
from orchestrator_test_helpers.unit.promote_support import STATES, FakeLinear, ago, instructions


class TestFailures1(TestFailuresBase):
    def test_error_on_one_issue_others_still_run(self):
        self.ready("DR-1")
        other = self.ready("DR-2")
        self.fake.fail[promote.Q_DETAIL, "DR-1"] = "boom"
        self.run_main()
        self.assertEqual(other["state"], "Done")
        self.assertEqual(self.fake.issues["DR-1"]["state"], "Handoff")
        self.assertIn("handoff-error DR-1", self.out)

    def test_failing_over_grace_bounces(self):
        for fail, existing, posted in (
            (promote.M_CREATE, False, "Handoff failed: linear api error: create failed"),
            (
                promote.M_RELATE,
                True,
                "Handoff failed: linear api error: relate failed The "
                "next-stage issue C-9 already exists.",
            ),
        ):
            with self.subTest(existing=existing):
                self.fake = FakeLinear()
                src = self.ready(handoff=90)
                if existing:
                    cid = promote.child_id("DR-1", "pm", ago(90))
                    self.fake.children[cid] = {"id": cid, "identifier": "C-9"}
                self.fake.fail[fail] = (
                    "create failed" if fail == promote.M_CREATE else "relate failed"
                )
                self.run_main()
                self.assertEqual(
                    (src["state"], src["posted"], src["subscribers"]),
                    ("In Review", [posted], ["me@x.com"]),
                )

    def test_grace_counts_from_latest_handoff(self):
        src = self.ready(handoff=2900)
        self.fake.moved("DR-1", 2800, "In Review", frm=STATES["Handoff"])
        self.fake.said("DR-1", 20, body="again")
        self.fake.moved("DR-1", 10, "Handoff", frm=STATES["In Review"])
        self.fake.fail[promote.M_CREATE] = "create failed"
        self.run_main()
        self.assertEqual(src["state"], "Handoff")
        self.assertNotIn("posted", src)
        self.fake.fail.clear()
        self.run_main()
        (child,) = self.fake.children.values()
        self.assertEqual(
            instructions(child["description"]),
            f"## Instructions\nMe, {ago(2915)}:\nbuild X\n\nMe, {ago(20)}:\nagain",
        )
        self.assertEqual(child["id"], promote.child_id("DR-1", "pm", ago(2900)))
        self.assertEqual(src["state"], "Done")
