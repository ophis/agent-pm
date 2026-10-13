import os

from agent_pm.linear_orchestrator.engine import promote
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.board_ids import TEAM
from orchestrator_test_helpers.unit.outage import FAILURES, FIELDS, failing
from orchestrator_test_helpers.unit.promote.outage_fixtures import Outage as OutageBase
from orchestrator_test_helpers.unit.promote_support import FakeLinear, FakePruner
from orchestrator_test_helpers.unit.run_fixtures import HARNESS


class Outage1(OutageBase):
    def test_a_failure_at_setup_is_one_linear_error_a_day(self):
        src = self.ready()
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                for said in (f"linear-error op=teams {FIELDS[name]}", ""):
                    gql = failing(error)
                    self.assertEqual(self.run_main(gql=gql), 1)
                    self.assertEqual((self.out, self.err, FakePruner.calls), (said, "", []))
                    self.assertEqual(gql.failed, [(linear.Q_TEAM, {"service": HARNESS, "t": TEAM})])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual((self.run_main(), src["state"]), (0, "Done"))

    def test_a_dry_run_prints_it_and_writes_nothing(self):
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                self.assertEqual(self.run_main("--dry-run", gql=failing(error)), 1)
                self.assertEqual(
                    (self.out, len(self.err.splitlines()), FakePruner.calls),
                    (f"linear-error op=teams {FIELDS[name]}", 1, []),
                )
        self.assertFalse(os.path.exists(self.logs))

    def test_a_failure_on_a_later_issue_stops_the_run(self):
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                self.fake = fake = FakeLinear()
                first, later = self.ready("DR-1", handoff=90), self.ready("DR-2", handoff=90)
                down = failing(error, gql=fake, ops={"issue"})
                self.assertEqual(
                    self.run_main(
                        gql=lambda q, **v: (down if v.get("i") == "DR-2" else fake)(q, **v)
                    ),
                    1,
                )
                self.assertEqual(
                    (self.out, self.err, FakePruner.calls),
                    (f"linear-error op=issue {FIELDS[name]}", "", []),
                )
                self.assertIn((promote.Q_DETAIL, {"i": "DR-1"}), fake.calls)
                self.assertEqual(
                    down.failed, [(promote.Q_DETAIL, {"service": HARNESS, "i": "DR-2"})]
                )
                self.assertEqual(
                    (first["state"], later["state"], fake.mutations), ("Handoff", "Handoff", [])
                )

    def test_a_failure_mid_promote_past_grace_bounces_nothing_and_the_next_run_promotes_once(self):
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                self.fake = FakeLinear()
                src = self.ready(handoff=90)
                gql = failing(error, gql=self.fake, ops={"issueRelationCreate"})
                self.assertEqual(self.run_main(gql=gql), 1)
                self.assertEqual(
                    (self.out, self.err, FakePruner.calls),
                    (f"linear-error op=issueRelationCreate {FIELDS[name]}", "", []),
                )
                self.assertEqual(
                    (src["state"], src["relations"], "posted" in src), ("Handoff", [], False)
                )
                self.assertEqual((self.run_main(), self.out), (0, "promote DR-1 to=C-1"))
                (child,) = self.fake.children.values()
                self.assertEqual(
                    (src["state"], src["relations"], src["posted"]),
                    ("Done", [child["id"]], ["Promoted to C-1."]),
                )
                self.assertEqual(
                    [q for q, _ in self.fake.mutations],
                    [promote.M_CREATE, promote.M_RELATE, linear.M_STATE, linear.M_COMMENT],
                )

    def test_a_failure_bouncing_a_failed_handoff_stops_the_run(self):
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                self.fake = FakeLinear()
                src = self.ready(handoff=90)
                self.fake.fail[promote.M_CREATE] = "create failed"
                self.assertEqual(
                    self.run_main(gql=failing(error, gql=self.fake, ops={"issueUpdate"})), 1
                )
                self.assertEqual(
                    self.out.splitlines(),
                    [
                        "handoff-error DR-1 error=LinearError: linear api error: create failed",
                        f"linear-error op=issueUpdate {FIELDS[name]}",
                    ],
                )
                self.assertEqual(
                    (src["state"], "posted" in src, self.err, FakePruner.calls),
                    ("Handoff", False, "", []),
                )
