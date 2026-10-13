from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.router.data import ago
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear


class LostClaim(FakeLinear):
    def __init__(self, issues):
        super().__init__(issues)
        self.lose = True

    def __call__(self, query, **v):
        out = super().__call__(query, **v)
        if query == linear.M_STATE and self.lose:
            self.lose = False
            self.issues[v["i"]]["updatedAt"] = ago(minutes=0)
            raise linear.Unavailable("issueUpdate", reason="TimeoutError: timed out")
        return out
