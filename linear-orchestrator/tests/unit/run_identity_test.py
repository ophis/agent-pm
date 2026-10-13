from agent_pm.delegate import Progress
from agent_pm.linear_orchestrator.engine import config
from agent_pm.linear_orchestrator.linear import sessions
from orchestrator_test_helpers.unit.run.data import cid
from orchestrator_test_helpers.unit.run.identity_fixtures import Identity as IdentityBase
from orchestrator_test_helpers.unit.run_fixtures import HARNESS, KEY, SESSION


class Identity1(IdentityBase):
    def test_session_comments_go_as_the_harness_and_write_back_as_the_role(self):
        self.done(Progress("start", "x"))
        self.assertEqual(self.main(mode="resume"), 0)
        self.assertEqual(config.load().harness_key, HARNESS)
        by = {}
        for (_, service, v), timeout in zip(self.gql.calls, self.gql.timeouts, strict=True):
            by.setdefault(v.get("c") == SESSION, set()).add((service, timeout))
        self.assertEqual(by, {True: {(HARNESS, sessions.LIMIT)}, False: {(KEY, 30)}})
        for step in ("start", "comment"):
            self.assertIn(("find", KEY, {"c": cid(step)}), self.gql.calls)
