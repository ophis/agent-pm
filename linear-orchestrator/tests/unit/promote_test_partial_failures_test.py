from agent_pm.linear_orchestrator.engine import promote
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.promote.test_partial_failures_fixtures import (
    TestPartialFailures as TestPartialFailuresBase,
)
from orchestrator_test_helpers.unit.promote_support import STATES


class TestPartialFailures1(TestPartialFailuresBase):
    def test_comment_failure_after_done_keeps_done(self):
        src = self.ready(handoff=90)
        self.fake.fail[linear.M_COMMENT] = "comment failed"
        self.run_main()
        self.assertEqual(src["state"], "Done")
        self.assertEqual(
            self.out.splitlines()[-1],
            "handoff-error DR-1 reason=promoted, but the comment failed "
            "error=LinearError: linear api error: comment failed",
        )

    def test_failed_bounce_posts_no_comment_and_stays_in_handoff(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["In Review"])
        self.fake.fail[linear.M_STATE] = "boom"
        self.run_main()
        self.assertEqual(src["state"], "Handoff")
        self.assertNotIn("posted", src)
        self.assertIn("handoff-error DR-1", self.out)

    def test_subscribe_failure_noted_in_the_bounce(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["In Review"])
        self.fake.fail[linear.M_SUBSCRIBE] = "boom"
        self.run_main()
        self.assertEqual(
            (src["state"], src["posted"]),
            (
                "In Review",
                [
                    promote.NO_INSTRUCTIONS
                    + "\n\nCould not subscribe me@x.com: LinearError: linear api error: boom"
                ],
            ),
        )
        self.assertIn("handoff-bounce DR-1 to=in_review reason=no instructions", self.out)

    def test_skipped_done_move_posts_no_comment(self):
        src = self.ready()
        self.moved_after_detail("In Review")
        self.run_main()
        self.assertEqual(src["state"], "In Review")
        self.assertNotIn("posted", src)
        self.assertEqual(
            self.fake.writes(), [promote.M_CREATE, promote.M_RELATE, linear.Q_ISSUE_STATE]
        )
        self.assertEqual(self.out, f"move-skip DR-1 step=promote state={STATES['In Review']}")

    def test_skipped_bounce_posts_no_comment(self):
        src = self.fake.add("DR-1")
        self.fake.moved("DR-1", 30, "Handoff", frm=STATES["In Review"])
        self.moved_after_detail("Todo")
        self.run_main()
        self.assertEqual((src["state"], src["subscribers"]), ("Todo", ["me@x.com"]))
        self.assertNotIn("posted", src)
        self.assertEqual(self.fake.writes(), [linear.M_SUBSCRIBE, linear.Q_ISSUE_STATE])
        self.assertEqual(self.out, f"move-skip DR-1 step=handoff-bounce state={STATES['Todo']}")
