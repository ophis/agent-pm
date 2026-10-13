import functools
import os

from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_test_helpers.unit.board_ids import TEAM
from orchestrator_test_helpers.unit.promote.test_prune_hook_fixtures import (
    TestPruneHook as TestPruneHookBase,
)
from orchestrator_test_helpers.unit.promote_support import NOW, ROLE, FakePruner
from orchestrator_test_helpers.unit.run_fixtures import HARNESS


class TestPruneHook1(TestPruneHookBase):
    def test_prune_runs_each_tick(self):
        self.run_main("--dry-run")
        self.assertEqual(len(FakePruner.calls), 1)
        gql, now, dry, work, team, roles = FakePruner.calls[0]
        self.assertIsInstance(gql, functools.partial)
        self.assertEqual((gql.func, gql.keywords), (self.fake, {"service": HARNESS}))
        self.assertEqual((now, dry, work), (NOW, True, os.path.join(self.work, "work")))
        self.assertEqual(team, linear.Team(TEAM, "Team", dict(IDS_BY_KEY)))
        self.assertEqual(roles, {f"u-{role}": role for role in ROLE})

    def test_prune_error_does_not_break_promote(self):
        class Boom:
            def __init__(self, *a, **kw):
                pass

            def run(self):
                raise RuntimeError("boom")

        self.ready("DR-1")
        for name, pruner in (("run error", Boom),):
            with self.subTest(name):
                rc = self.run_main("--dry-run", pruner=pruner)
                self.assertEqual(rc, 0)
                self.assertEqual(
                    self.out.splitlines()[0], "promote DR-1 role=pm project=Deep Research"
                )
                self.assertRegex(
                    self.out.splitlines()[1],
                    r"^prune-error error=RuntimeError: boom",
                )
