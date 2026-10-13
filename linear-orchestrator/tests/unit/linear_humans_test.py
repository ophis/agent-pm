from types import SimpleNamespace

from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.humans_fixtures import Humans as HumansBase


class Humans1(HumansBase):
    def test_humans(self):
        def gql(_q, **v):
            return {"users": {"nodes": [{"id": "u-" + v["e"]}] if v["e"] != "x@y.z" else []}}

        self.assertEqual(linear.humans(gql, SimpleNamespace(human_members=("a@b.c",))), ["u-a@b.c"])
        self.assertEqual(linear.humans(gql, SimpleNamespace(human_members=())), [])
        with self.assertRaises(LinearError) as cm:
            linear.humans(gql, SimpleNamespace(human_members=("a@b.c", "x@y.z")))
        self.assertEqual(
            str(cm.exception),
            "linear-orchestrator/config.toml: human_members not found in Linear: x@y.z",
        )
