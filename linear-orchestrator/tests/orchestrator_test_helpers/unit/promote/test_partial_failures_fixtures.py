
from agent_pm.linear_orchestrator.engine import promote
from orchestrator_test_helpers.unit.promote_support import Base


class TestPartialFailures(Base):
    def moved_after_detail(self, state):
        fake = self.fake

        def gql(query, **v):
            out = fake(query, **v)
            if query == promote.Q_DETAIL:
                fake.issues[v["i"]]["state"] = state
            return out

        gql.writes = fake.writes
        self.fake = gql
