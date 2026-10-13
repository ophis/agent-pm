from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.board_ids import TEAM
from orchestrator_test_helpers.unit.linear.outage_fixtures import Outage as OutageBase
from orchestrator_test_helpers.unit.outage import FAILURES, failing


class Outage1(OutageBase):
    def test_each_failure_is_unavailable_and_recorded(self):
        want = {
            "5xx": (503, None),
            "timeout": (None, "TimeoutError: timed out"),
            "URLError": (None, "ConnectionRefusedError: [Errno 61] Connection refused"),
        }
        self.assertEqual(set(FAILURES), set(want))
        for name, error in FAILURES.items():
            with self.subTest(name):
                gql = failing(error)
                for _ in range(2):
                    with self.assertRaises(linear.Unavailable) as cm:
                        gql(linear.Q_TEAM, t=TEAM)
                    self.assertEqual(
                        (cm.exception.op, cm.exception.status, cm.exception.reason),
                        ("teams", *want[name]),
                    )
                self.assertEqual(gql.failed, [(linear.Q_TEAM, {"t": TEAM})] * 2)

    def test_ops_names_the_failing_operations(self):
        gql = failing(
            FAILURES["5xx"], gql=lambda q, **v: {"q": q, **v}, ops={"teams", "issueUpdate"}
        )
        self.assertEqual(gql(linear.Q_ISSUE_STATE, i="I"), {"q": linear.Q_ISSUE_STATE, "i": "I"})
        for query in (linear.Q_TEAM, linear.M_STATE):
            with self.assertRaises(linear.Unavailable):
                gql(query, i="I")
        self.assertEqual(gql.failed, [(linear.Q_TEAM, {"i": "I"}), (linear.M_STATE, {"i": "I"})])
