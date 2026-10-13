from agent_pm.linear_orchestrator.linear import sessions
from orchestrator_test_helpers.unit.sessions.case_fixtures import Case
from orchestrator_test_helpers.unit.sessions.data import ISSUE, ISSUE_ID, T1, T2
from orchestrator_test_helpers.unit.sessions.fake_linear_fixtures import FakeLinear


class Repair(Case):
    DONE = f"{T1} → {T2} · exit 0"

    def setUp(self):
        super().setUp()
        self.fake, self.rec = FakeLinear(), self.record()

    def repair(self, gql=None, **kw):
        return sessions.repair(ISSUE, ISSUE_ID, self.rec, gql=gql or self.fake, **kw)
