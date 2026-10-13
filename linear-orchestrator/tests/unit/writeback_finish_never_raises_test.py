from agent_pm.linear_orchestrator.engine.types import Stopped
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit.writeback.data import ID, outcome
from orchestrator_test_helpers.unit.writeback.finish_never_raises_fixtures import (
    FinishNeverRaises as FinishNeverRaisesBase,
)
from orchestrator_test_helpers.unit.writeback.gql_fixtures import Gql
from orchestrator_test_helpers.unit.writeback_support import PR


class FinishNeverRaises1(FinishNeverRaisesBase):
    def test_unknown_role_logged(self):
        self.assertFalse(writeback.finish(self.ctx("nope"), outcome("done", summary="x")))
        self.assertEqual(self.lines(), [f"step-error {ID} step=finish error=KeyError: 'nope'"])

    def test_signal_exit_stops_the_steps(self):
        for name in ("read", "subscribe", "comment", "attach"):
            with self.subTest(name=name):
                gql = Gql(fail=lambda n, _v: Stopped(143) if n == name else None)
                ctx = self.ctx(gql=gql)
                self.assertFalse(
                    writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR))
                )
                self.assertEqual(gql.calls[-1][0], name)
                self.assertEqual(
                    self.lines()[-1], f"step-error {ID} step=finish error=Stopped: 143"
                )
                self.assertNotIn("state", [c[0] for c in gql.calls])
