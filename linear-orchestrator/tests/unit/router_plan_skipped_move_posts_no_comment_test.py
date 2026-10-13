from orchestrator_test_helpers.unit.router.data import STATES, ago, issue, writes
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear
from orchestrator_test_helpers.unit.router.moved_fixtures import Moved
from orchestrator_test_helpers.unit.router.plan_fixtures import Plan as PlanBase


class Plan2(PlanBase):
    def test_skipped_move_posts_no_comment(self):
        fake = Moved(
            [issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))], "In Review"
        )
        self.assertEqual(self.plan(fake), "")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("In Review", []))
        self.assertNotIn("comments", fake.issues["TASK-1"])
        self.assertEqual(writes(fake), [("read", "TASK-1")])
        self.assertIn(f"move-skip TASK-1 step=recover state={STATES['In Review']}", self.said())

    def test_one_history_fetch_per_issue(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        queries = []
        self.plan(lambda q, **v: queries.append(q) or fake(q, **v))
        self.assertEqual(sum("history" in q for q in queries), 1)
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
