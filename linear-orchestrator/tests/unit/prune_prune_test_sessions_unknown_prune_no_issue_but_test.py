import os
from unittest import mock

from agent_pm.linear_orchestrator.engine import prune
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.session import ConfigError, DriverError
from orchestrator_test_helpers.unit import promote_support as tp
from orchestrator_test_helpers.unit.prune.data import A, B, gql_for
from orchestrator_test_helpers.unit.prune.prune_test_fixtures import PruneTest as PruneTestBase
from orchestrator_test_helpers.unit.run_fixtures import HARNESS


class PruneTest3(PruneTestBase):
    def test_sessions_unknown_prune_no_issue_but_archive(self):
        for dry, archived in (
            (False, "prune-archived TASK-49"),
            (True, "prune-plan TASK-49 what=archive"),
        ):
            with self.subTest(dry=dry):
                self.fresh()
                self.api.order.clear()
                kept = [self.mkc("TASK-49", "src", "o", "repo"), self.mkc("TASK-49", "publish")]
                self.api.live = None
                gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
                code, out = self.prune(gql, dry=dry)
                self.assertEqual(
                    (code, out.splitlines()),
                    (3, ["prune-error error=tui sessions unknown", archived]),
                )
                self.assertTrue(all(os.path.isdir(os.path.join(p, ".git")) for p in kept))
                self.assertNotIn(prune.Q_ISSUE, gql.calls)
                self.assertEqual(gql.archived, [] if dry else ["id-TASK-49"])
                self.assertEqual(self.api.order, [("sessions", None)])

    def test_close_failure_counted(self):
        for error in (DriverError("boom"), ConfigError("bad name")):
            with self.subTest(error=type(error).__name__):
                self.api.order.clear()
                self.api.live, self.api.errors = [A, B], {B: error}
                code, out = self.prune(gql_for({"TASK-7": ("Done", [(30, "Done")])}))
                self.assertEqual(code, 3)
                self.assertEqual(
                    out.splitlines(),
                    [
                        f"prune-error TASK-7 session={B} error={type(error).__name__}: {error}",
                        f"prune-closed TASK-7 session={A}",
                    ],
                )
                self.assertEqual(self.closed(), [B, A])

    def test_promote_tick_archives_with_its_roles(self):
        gql = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        code, _, out = self.tick(gql)
        self.assertEqual((code, gql.archived), (0, ["id-TASK-1"]))
        self.assertIn("prune-archived TASK-1", out)
        self.assertEqual(self.user_queries, len(tp.ROLE))

    def test_prune_failure_leaves_handoff_alone(self):
        clone = self.mkc("TASK-49", "src", "o", "repo")

        def down(_query, **_v):
            raise LinearError("linear api error: down")

        code, state, out = self.tick(down)
        self.assertEqual((code, state), (0, "Done"))
        self.assertIn("prune-error TASK-49 error=Linear: LinearError: linear api error: down", out)
        self.assertTrue(os.path.isdir(clone))

    def test_an_outage_reading_an_issue_stops_prune(self):
        clones = [self.mkc(ident, "src", "o", "repo") for ident in ("TASK-48", "TASK-49")]
        issues = gql_for(dict.fromkeys(("TASK-48", "TASK-49"), ("Done", [(30, "Done")])))
        self.outage("issue", issues, [(prune.Q_ISSUE, {"service": HARNESS, "i": "TASK-48"})])
        self.assertEqual(issues.calls, [])
        self.assertTrue(all(os.path.isdir(c) for c in clones))

    def test_an_outage_reading_the_finished_issues_stops_prune(self):
        issues = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        self.outage("issues", issues, [(prune.Q_FINISHED, mock.ANY)])

    def test_an_outage_archiving_stops_prune(self):
        done = ("Done", [(30, "Done")])
        issues = gql_for(
            {"TASK-1": done, "TASK-2": done}, owners={"TASK-1": "u-pm", "TASK-2": "u-pm"}
        )
        self.outage(
            "issueArchive", issues, [(prune.M_ARCHIVE, {"service": HARNESS, "i": "id-TASK-1"})]
        )
