from agent_pm.linear_orchestrator.linear import issues
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear


class ClaimLinear(FakeLinear):
    """FakeLinear plus the outer's issue read, answered with `node`."""

    def __init__(self, todo, node):
        super().__init__([todo])
        self.node = node

    def __call__(self, query, **v):
        if query == issues.Q_ISSUE:
            self.queries.append((query, v))
            return {"issue": self.node}
        return super().__call__(query, **v)
