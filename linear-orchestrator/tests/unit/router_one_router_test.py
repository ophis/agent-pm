import io
from contextlib import redirect_stdout

from agent_pm.linear_orchestrator.engine import router
from orchestrator_test_helpers.unit.router.data import issue, other_router
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear
from orchestrator_test_helpers.unit.router.one_router_fixtures import OneRouter as OneRouterBase


class OneRouter1(OneRouterBase):
    def test_a_tick_skips_while_another_router_runs(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        before = self.before()
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with other_router(self.runs):
            self.assertEqual(self.tick(fake), 0)
            self.assertEqual(self.said(), ["skip reason=another router is running"])
            self.assertEqual(
                (fake.queries, self.api.order, self.state, fake.issues["TASK-1"]["state"]),
                ([], [], before, "Todo"),
            )
            self.assertEqual((self.tick(fake, hour=12), self.said(), self.api.order), (0, [], []))
        self.assertEqual(self.tick(fake), 0)
        self.assertEqual(self.launched_runs(), [("TASK-1", "new")])
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_another_router_leaves_the_issue_alone(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with other_router(self.runs):
            self.assertEqual(self.tick(fake, "--issue", "TASK-1"), 1)
        self.assertEqual(self.err, "router.py: another router is running; try again\n")
        self.assertEqual(self.said(), ["skip TASK-1 reason=another router is running"])
        self.assertEqual((fake.queries, self.api.order, self.state), ([], [], ""))

    def test_a_dry_run_and_the_brake_take_no_lock(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with other_router(self.runs):
            self.assertEqual(self.tick(fake, "--dry-run"), 0)
            self.assertEqual(self.said()[:-1], ["plan mode=new queue=1", "pick TASK-1 queue=1"])
            self.assertRegex(self.said()[-1], "^usage runtime=a ")
            self.assertEqual(self.tick(fake, "--dry-run", "--issue", "TASK-1"), 0)
            self.assertEqual(self.said(), ["pick TASK-1 queue=1"])
            out = io.StringIO()
            with redirect_stdout(out):
                rc = router.main(
                    ["--brake", "researcher"], config_path=self.config_path, driver=self.api
                )
            self.assertEqual((rc, out.getvalue()), (0, "five_hour=10% weekly=10%\n"))
        self.assertEqual(fake.mutations, [])
