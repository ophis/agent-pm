
from orchestrator_test_helpers.unit.sessions.case_fixtures import Case


class Failure(Case):
    def failing(self, exc):
        def gql(_query, **_v):
            raise exc

        return gql
