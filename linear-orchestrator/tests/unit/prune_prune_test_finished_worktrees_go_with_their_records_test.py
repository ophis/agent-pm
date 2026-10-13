import os
import shutil
import subprocess
from unittest import mock

from agent_pm.linear_orchestrator.engine import prune
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.utils import temp_dirs
from orchestrator_test_helpers.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_test_helpers.unit.prune.data import (
    NOW,
    ROLES,
    TEAM_OBJ,
    A,
    B,
    git,
    gql_for,
    write,
)
from orchestrator_test_helpers.unit.prune.git_fixtures import Git
from orchestrator_test_helpers.unit.prune.prune_test_fixtures import PruneTest as PruneTestBase


class PruneTest2(PruneTestBase):
    def test_finished_worktrees_go_with_their_records_and_pushed_branches(self):
        with self.git_env():
            clone = self.real_clone()
            x, y = (
                os.path.join(self.work, "TASK-49", "src", "o", "n-TASK-49"),
                os.path.join(self.work, "TASK-49", "src", "p", "m-TASK-49"),
            )
            git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-x", x, "origin/main")
            git("-C", x, "commit", "-q", "--allow-empty", "-m", "x")
            git("-C", x, "push", "-q", "origin", "TASK-49-x")
            git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-y", y, "origin/main")
            git("-C", y, "commit", "-q", "--allow-empty", "-m", "y")
            git("-C", clone, "branch", "TASK-49-z", "origin/main")
            git("-C", clone, "branch", "TASK-490-q", "origin/main")
            own = os.path.join(self.root, "own")
            git("-C", clone, "worktree", "add", "-q", "-b", "mine", own)
            git("-C", clone, "worktree", "lock", own)
            shutil.move(own, own + "-unmounted")
            write(os.path.join(clone, "dirty.txt"), "x")
            before = [
                git("-C", clone, *a)
                for a in (
                    ["rev-parse", "HEAD"],
                    ["branch", "--show-current"],
                    ["status", "--porcelain"],
                )
            ]
            run = Git(real=True)
            code, out = self.prune(
                gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=run, writable=(self.work,)
            )
            self.assertEqual(code, 0, out)
            self.assertEqual(
                out.splitlines(),
                [
                    "prune-removed entry=TASK-49/src/o/n-TASK-49 what=worktree",
                    "prune-removed entry=TASK-49/src/p/m-TASK-49 what=worktree",
                    f"prune-removed TASK-49 what=branch TASK-49-x in {clone}",
                    f"prune-skip TASK-49 reason=branch TASK-49-y in {clone} kept: not pushed",
                    f"prune-removed TASK-49 what=branch TASK-49-z in {clone}",
                ],
            )
            self.assertFalse(os.path.lexists(x) or os.path.lexists(y))
            listing = git("-C", clone, "worktree", "list", "--porcelain")
            self.assertNotIn(x, listing)
            self.assertNotIn(y, listing)
            self.assertIn(f"worktree {own}", listing)
            self.assertEqual(
                sorted(git("-C", clone, "branch", "--format=%(refname:short)").split()),
                ["TASK-49-y", "TASK-490-q", "main", "mine"],
            )
            self.assertEqual(
                [
                    git("-C", clone, *a)
                    for a in (
                        ["rev-parse", "HEAD"],
                        ["branch", "--show-current"],
                        ["status", "--porcelain"],
                    )
                ],
                before,
            )
            self.assertTrue(run.calls)
            self.assertTrue(
                all(c[5:8] == ["-C", clone, f"--git-dir={clone}/.git"] for c in run.calls),
                run.calls,
            )

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

        def failing(argv, _timeout):
            return subprocess.CompletedProcess(argv, 1, "", "fatal: boom")

        code, out = self.prune(
            gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=failing, writable=(self.work,)
        )
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines()[0], "prune-removed entry=TASK-49/src/o/n what=worktree")
        self.assertTrue(
            out.splitlines()[1].startswith(
                "prune-error TASK-49 error=GitError: git --git-dir="
            ),
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
                    f"prune-plan TASK-49 what=prune {clone}'s worktree records and pushed TASK-49-* branches",
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
