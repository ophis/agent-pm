from types import SimpleNamespace

from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.board_ids import STATES, TEAM


class TeamCheck(Case):
    def cfg(self):
        return SimpleNamespace(team=TEAM, states=dict(STATES))

    def gql(self, nodes):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            return {"teams": {"nodes": nodes}}

        gql.calls = calls
        return gql
