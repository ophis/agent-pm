import os
import subprocess
from unittest import mock

from agent_pm.linear_orchestrator.engine import config, prune
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.session import ConfigError, DriverError
from agent_pm.utils import temp_dirs
from orchestrator_tests.case import Case
from orchestrator_tests.unit import promote_support as tp
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.prune_data import NOW, ROLES, TEAM_OBJ, A, B, git, gql_for
from orchestrator_tests.unit.prune_scenarios import Git, PruneScenario
from orchestrator_tests.unit.run_fixtures import HARNESS


class PruneACloneAnAgentRun(PruneScenario):
    def test_a_clone_an_agent_run_could_write_gets_no_git_run(self):
        with self.git_env():
            for name, at, writable in (
                ("in work/", os.path.join(self.work, "TASK-49", "evil"), (self.work,)),
                ("in a temp dir, by default", os.path.join(self.root, "tmp-clone"), None),
            ):
                with self.subTest(name):
                    clone = self.real_clone(at)
                    wt = os.path.join(self.work, "TASK-49", "src", "o", "n")
                    git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-x", wt, "origin/main")
                    run = Git(real=True)
                    code, out = self.prune(
                        gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=run, writable=writable
                    )
                    self.assertEqual((code, run.calls), (0, []))
                    self.assertEqual(
                        out.splitlines()[0], "prune-removed entry=TASK-49/src/o/n what=worktree"
                    )
                    self.assertTrue(
                        out.splitlines()[1].startswith(
                            f"prune-skip TASK-49 reason={clone} is under "
                        ),
                        out,
                    )
                    self.assertFalse(os.path.lexists(wt))

    def test_writable_dirs_default_to_work_and_the_temp_dirs(self):
        pruner = prune.Pruner(
            None, NOW, False, work=self.work, driver=self.api, team=TEAM_OBJ, roles=ROLES
        )
        self.assertEqual(pruner.writable, (self.work, *temp_dirs()))

    def test_a_git_failure_in_the_clone_is_an_error(self):
        with self.git_env():
            clone = self.real_clone()
            git(
                "-C",
                clone,
                "worktree",
                "add",
                "-q",
                "-b",
                "TASK-49-x",
                os.path.join(self.work, "TASK-49", "src", "o", "n"),
            )

        def failing(argv, timeout):
            return subprocess.CompletedProcess(argv, 1, "", "fatal: boom")

        code, out = self.prune(
            gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=failing, writable=(self.work,)
        )
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines()[0], "prune-removed entry=TASK-49/src/o/n what=worktree")
        self.assertTrue(
            out.splitlines()[1].startswith("prune-error TASK-49 error=GitError: git --git-dir="),
            out,
        )
        self.assertTrue(out.splitlines()[1].endswith("worktree prune: fatal: boom"), out)

    def test_dry_run_plans_the_clone_step_and_runs_no_git(self):
        with self.git_env():
            clone = self.real_clone()
            wt = os.path.join(self.work, "TASK-49", "src", "o", "n")
            git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-x", wt)
        code, out = self.prune(
            gql_for({"TASK-49": ("Done", [(30, "Done")])}), dry=True, writable=(self.work,)
        )
        self.assertEqual(
            (code, out.splitlines()),
            (
                0,
                [
                    "prune-plan entry=TASK-49/src/o/n what=delete the worktree",
                    (
                        f"prune-plan TASK-49 what=prune {clone}"
                        f"'s worktree records and pushed TASK-49-* branches"
                    ),
                ],
            ),
        )
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))

    def test_archives_pm_and_engineer_issues_finished_24h(self):
        gql = gql_for(
            {
                "TASK-1": ("Done", [(30, "In Progress"), (24, "Done")]),
                "TASK-2": ("Canceled", [(50, "Canceled")]),
                "TASK-3": ("Done", [(30, "Done")]),
                "TASK-4": ("Canceled", [(24, "Canceled")]),
            },
            owners={
                "TASK-1": "u-pm",
                "TASK-2": "u-pm",
                "TASK-3": "u-engineer",
                "TASK-4": "u-engineer",
            },
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3", "id-TASK-4"])
        self.assertEqual(
            out.splitlines(),
            [
                "prune-archived TASK-1",
                "prune-archived TASK-2",
                "prune-archived TASK-3",
                "prune-archived TASK-4",
            ],
        )

    def test_not_archived(self):
        done = ("Done", [(50, "Done")])
        gql = gql_for(
            {
                "TASK-1": ("Done", [(24 - 1 / 3600, "Done")]),
                "TASK-2": ("Done", [(50, "Done"), (30, "In Progress"), (24 - 1 / 3600, "Done")]),
                "TASK-3": ("Done", []),
                "TASK-4": ("In Progress", [(50, "Done"), (30, "In Progress")]),
                "TASK-5": done,
                "TASK-6": done,
                "TASK-7": done,
            },
            owners={
                "TASK-1": "u-pm",
                "TASK-2": "u-engineer",
                "TASK-3": "u-pm",
                "TASK-4": "u-pm",
                "TASK-5": "u-researcher",
                "TASK-6": "u-other",
                "TASK-7": None,
            },
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, [])
        self.assertEqual(
            out.splitlines(), ["prune-skip TASK-3 reason=archive: finish time unknown"]
        )
        (v,) = gql.finished
        self.assertEqual(sorted(v["a"]), ["u-engineer", "u-pm"])
        self.assertEqual(sorted(v["s"]), sorted([IDS_BY_KEY["done"], IDS_BY_KEY["canceled"]]))

    def test_archive_failure_others_archived(self):
        done = ("Done", [(30, "Done")])
        gql = gql_for(
            {"TASK-1": done, "TASK-2": done, "TASK-3": done},
            owners=dict.fromkeys(("TASK-1", "TASK-2", "TASK-3"), "u-pm"),
            fail={"TASK-1"},
            refuse={"TASK-2"},
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3"])
        self.assertEqual(
            out.splitlines(),
            [
                "prune-error TASK-1 error=archive: LinearError: linear api error: boom",
                "prune-error TASK-2 error=archive: LinearError: issueArchive: success: false",
                "prune-archived TASK-3",
            ],
        )

    def test_archive_reads_every_page_first(self):
        done = ("Done", [(30, "Done")])
        idents = ("TASK-1", "TASK-2", "TASK-3")
        gql = gql_for(
            dict.fromkeys(idents, done), owners=dict.fromkeys(idents, "u-engineer"), page=2
        )
        self.assertEqual(self.prune(gql)[0], 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3"])
        self.assertEqual([v["c"] for v in gql.finished], [None, "2"])

    def test_candidate_query_failure_keeps_clone_step(self):
        clone = self.mkc("TASK-49", "src", "o", "repo")
        issues = gql_for({"TASK-49": ("Done", [(30, "Done")])})

        def gql(query, **v):
            if query == prune.Q_FINISHED:
                raise LinearError("linear api error: down")
            return issues(query, **v)

        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertIn("prune-error error=archive: Linear: LinearError: linear api error: down", out)
        self.assertFalse(os.path.lexists(clone))

    def test_a_live_tui_session_alone_makes_a_candidate_closed_when_finished_24h(self):
        self.api.live = [A, B, "engineer-TASK-8-aaaaaaaa"]
        gql = gql_for(
            {"TASK-7": ("Done", [(24, "Done")]), "TASK-8": ("In Progress", [(30, "In Progress")])}
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [f"prune-closed TASK-7 session={B}", f"prune-closed TASK-7 session={A}"],
        )
        self.assertEqual(gql.calls.count(prune.Q_ISSUE), 2)
        self.assertEqual(self.closed(), [B, A])

    def test_only_names_of_the_contract_make_candidates_and_get_closed(self):
        others = [
            "agent-pm-engineer-TASK-7",
            "Engineer-TASK-8-aaaaaaaa",
            "engineer-build-7c1d9e2f",
            "notes",
        ]
        self.api.live = [*others, A, "engineer-TASK-70-bbbbbbbb"]
        gql = gql_for(
            {"TASK-7": ("Done", [(30, "Done")]), "TASK-70": ("In Progress", [(30, "In Progress")])}
        )
        code, out = self.prune(gql)
        self.assertEqual((code, out.splitlines()), (0, [f"prune-closed TASK-7 session={A}"]))
        self.assertEqual(self.closed(), [A])

    def test_only_configured_roles_tui_sessions_make_candidates_and_get_closed(self):
        self.api.live = ["designer-TASK-7-0c0c0c0c", A, "designer-TASK-9-0d0d0d0d"]
        done = ("Done", [(30, "Done")])
        gql = gql_for({"TASK-7": done, "TASK-9": done})
        code, out = self.prune(gql)
        self.assertEqual((code, out.splitlines()), (0, [f"prune-closed TASK-7 session={A}"]))
        self.assertEqual(self.closed(), [A])
        self.assertEqual(gql.calls.count(prune.Q_ISSUE), 1)

    def test_clone_and_session_issue_queried_once_sessions_first(self):
        clone = self.mkc("TASK-7", "src", "o", "repo")
        self.api.live = [A]
        seen, close = [], self.api.close
        gql = gql_for({"TASK-7": ("Canceled", [(30, "Canceled")])})
        with mock.patch.object(
            self.api, "close", lambda name: (seen.append(os.path.isdir(clone)), close(name))
        ):
            code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                f"prune-closed TASK-7 session={A}",
                "prune-removed entry=TASK-7/src/o/repo what=clone",
            ],
        )
        self.assertEqual((seen, self.closed()), ([True], [A]))
        self.assertEqual(gql.calls.count(prune.Q_ISSUE), 1)
        self.assertFalse(os.path.lexists(clone))

    def test_dry_run_plans_each_live_session_and_kills_none(self):
        self.api.live = [A, B, "engineer-TASK-70-bbbbbbbb"]
        code, out = self.prune(
            gql_for(
                {
                    "TASK-7": ("Done", [(30, "Done")]),
                    "TASK-70": ("In Progress", [(30, "In Progress")]),
                }
            ),
            dry=True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                f"prune-plan TASK-7 session={B} what=close the tui session",
                f"prune-plan TASK-7 session={A} what=close the tui session",
            ],
        )
        self.assertEqual(self.api.order, [("sessions", None)])

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

        def down(query, **v):
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


class Imports(Case):
    def test_clones_are_configs(self):
        self.assertIs(prune.CLONES, config.CLONES)
