import os
from datetime import timedelta

from agent_pm.linear_orchestrator.engine import router
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.board_ids import TEAM
from orchestrator_test_helpers.unit.outage import FAILURES, failing
from orchestrator_test_helpers.unit.router.data import NOW, ago, issue
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear
from orchestrator_test_helpers.unit.router.lost_claim_fixtures import LostClaim
from orchestrator_test_helpers.unit.router.outage_fixtures import Outage as OutageBase
from orchestrator_test_helpers.unit.run_fixtures import HARNESS


class Outage1(OutageBase):
    def test_a_failure_at_board_setup_is_one_linear_error_a_day(self):
        for argv in (("--now",), ("--issue", "TASK-1")):
            for name, error in FAILURES.items():
                with self.subTest(argv=argv, failure=name):
                    for again in (False, True):
                        gql = failing(error)
                        self.assertEqual(self.tick(gql, *argv, again=again), 1)
                        self.assertEqual(
                            (self.said(), self.raw), ([] if again else [self.SAID[name]], "")
                        )
                        self.assertEqual(
                            gql.failed, [(linear.Q_TEAM, {"service": HARNESS, "t": TEAM})]
                        )
                        self.assertEqual((self.start.launches, self.state), ([], ""))
                    fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                    self.assertEqual(self.tick(fake, *argv), 0)
                    self.assertEqual(self.launched_runs(), [("TASK-1", "new")])

    def test_a_dry_run_prints_it_and_writes_nothing(self):
        for argv in (("--now", "--dry-run"), ("--issue", "TASK-1", "--dry-run")):
            for name, error in FAILURES.items():
                with self.subTest(argv=argv, failure=name):
                    self.assertEqual(self.tick(failing(error), *argv), 1)
                    self.assertEqual(
                        (self.said(), len(self.raw.splitlines())), ([self.SAID[name]], 1)
                    )
                    self.assertEqual((os.listdir(self.logs), self.state), (["runs.jsonl"], ""))

    def test_a_claim_applied_with_its_answer_lost_is_recovered_once_stale(self):
        for argv, plan in ((("--now",), ["plan mode=new queue=1"]), (("--issue", "TASK-1"), [])):
            with self.subTest(argv=argv):
                fake = LostClaim([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv), 1)
                self.assertEqual(
                    self.said(),
                    plan
                    + [
                        "pick TASK-1 queue=1",
                        "claim TASK-1 role=researcher runtime=a",
                        "linear-error op=issueUpdate reason=TimeoutError: timed out",
                    ],
                )
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], self.start.launches, self.state),
                    ("In Progress", [], ""),
                )
                self.assertEqual(
                    self.tick(fake, "--now", now=NOW + router.STALE + timedelta(minutes=1)), 0
                )
                self.assertEqual(
                    self.said(),
                    [
                        f"recover TASK-1 to=todo reason=no session updated={ago(minutes=0)}",
                        "plan mode=new queue=1",
                        "pick TASK-1 queue=1",
                        "claim TASK-1 role=researcher runtime=a",
                        "launch TASK-1 exit=0",
                    ],
                )
                self.assertEqual(fake.issues["TASK-1"]["comments"], [router.INTERRUPTED])
