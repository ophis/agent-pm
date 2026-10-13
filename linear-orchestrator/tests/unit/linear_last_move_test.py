from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.board_ids import STATES
from orchestrator_test_helpers.unit.linear.data import IN_PROGRESS, IN_REVIEW, TODO
from orchestrator_test_helpers.unit.linear.last_move_fixtures import LastMove as LastMoveBase


class LastMove1(LastMoveBase):
    def test_latest_move_into_the_states_by_actors(self):
        at = linear.parse_time
        for nodes, states, actors, want in (
            (self.NODES, {TODO}, None, at("2026-09-27T12:00:00.000Z")),
            (self.NODES, {TODO, IN_REVIEW}, None, at("2026-09-27T12:00:00.000Z")),
            (self.NODES, {IN_REVIEW}, None, at("2026-09-27T11:00:00.000Z")),
            (self.NODES, {STATES["done"]}, None, None),
            ([], {TODO}, None, None),
            (self.NODES, {TODO}, {"human"}, at("2026-09-27T10:00:00.000Z")),
            (self.NODES, {IN_PROGRESS}, {"human", "agent"}, None),
            (self.NODES, {TODO}, set(), None),
        ):
            with self.subTest(states=states, actors=actors, nodes=len(nodes)):
                self.assertEqual(linear.last_move(nodes, states, actors), want)
