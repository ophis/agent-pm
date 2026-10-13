from types import SimpleNamespace

from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.board_ids import ACCOUNTS
from orchestrator_test_helpers.unit.linear.role_ids_fixtures import RoleIds as RoleIdsBase


class RoleIds1(RoleIdsBase):
    def test_role_ids(self):
        roles = {r: SimpleNamespace(account=a) for r, a in ACCOUNTS.items()}

        def gql(_query, **variables):
            return {"users": {"nodes": [{"id": "u-" + variables["e"]}]}}

        self.assertEqual(linear.role_ids(gql, roles), {f"u-{a}": r for r, a in ACCOUNTS.items()})
        with self.assertRaises(LinearError) as cm:
            linear.role_ids(
                lambda _q, **_v: {"users": {"nodes": []}}, {"engineer": roles["engineer"]}
            )
        self.assertEqual(
            str(cm.exception),
            "linear-orchestrator/config.toml [roles.engineer]: account 'engineer@agents.test' not found in Linear",
        )
