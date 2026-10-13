from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.board_ids import STATES, TEAM, team_node
from orchestrator_test_helpers.unit.linear.team_check_fixtures import TeamCheck as TeamCheckBase


class TeamCheck1(TeamCheckBase):
    def test_ok(self):
        gql = self.gql([team_node()])
        self.assertEqual(linear.team(gql, self.cfg()), linear.Team(TEAM, "Team", dict(STATES)))
        self.assertEqual(gql.calls, [(linear.Q_TEAM, {"t": TEAM})])

    def test_team_not_found_or_states_outside_team(self):
        other = [i for k, i in STATES.items() if k not in ("handoff", "done")]
        cases = {
            f"team {TEAM} not found in Linear": [],
            f"[states] not workflow states of team 'Team': handoff {STATES['handoff']}, done {STATES['done']}": [
                team_node(other)
            ],
        }
        for message, nodes in cases.items():
            with self.subTest(message):
                with self.assertRaises(LinearError) as cm:
                    linear.team(self.gql(nodes), self.cfg())
                self.assertEqual(
                    str(str(cm.exception)), "linear-orchestrator/config.toml: " + message
                )
