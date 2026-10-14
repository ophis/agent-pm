"""Orchestrator prune behavior at its dependency boundaries."""

import io
import json
import os
import shutil
from contextlib import redirect_stderr, redirect_stdout
from functools import partial
from unittest import mock

from agent_pm.delegate import ConfigError, Delegate, SessionError, Spec
from agent_pm.linear_orchestrator.engine import config, ops, prune, runs
from agent_pm.linear_orchestrator.engine import log as event_log
from agent_pm.linear_orchestrator.engine.types import Docs, LinearError
from agent_pm.utils import load_class, temp_dirs
from orchestrator_tests.case import Case
from orchestrator_tests.prune_fixtures import (
    NOW,
    ROLES,
    TEAM_OBJ,
    Git,
    PruneDelegate,
    PruneScenario,
    gql_for,
    write,
)
from orchestrator_tests.unit import promote_fixtures as tp
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.run_fixtures import CORE_LOCAL, HARNESS, logged, show

from delegate.tests.fakes.harness_session import HarnessSession
from delegate.tests.harness import HarnessCase

A, B = "engineer-TASK-7-0b6f2c1e", "dummy-tester-TASK-7-7c1d9e2f"
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
OTHER_SID = "7c1d9e2f-0a3b-4c5d-8e6f-1a2b3c4d5e6f"


class RepositoryCleanup(PruneScenario):
    def test_entries_that_are_no_clone_or_worktree_are_skipped_and_kept(self):
        rows = [
            (dot_git, make, parts)
            for dot_git, make in (
                ("none", partial(self.mkg, git=None)),
                ("symlinked", self.symlinked_dot_git),
            )
            for parts in (("src", "o", "bare"), ("publish",))
        ]
        for n, (dot_git, make, parts) in enumerate(rows, 41):
            with self.subTest(dot_git=dot_git, parts=parts):
                ident = f"TASK-{n}"
                self.fresh()
                path = make(ident, *parts)
                code, out = self.prune(gql_for({ident: ("Done", [(30, "Done")])}))
                self.assertEqual(code, 0)
                self.assertEqual(
                    out.splitlines(),
                    [f"prune-skip entry={ident}/{'/'.join(parts)} reason=not a clone or worktree"],
                )
                self.assertTrue(os.path.isdir(path))
                if dot_git == "symlinked":
                    self.assertTrue(os.path.isfile(os.path.join(path, "notes.md")))
                    self.assertTrue(os.path.isdir(os.path.join(self.outside, "dotgit")))

    def test_the_same_skip_twice_is_one_line(self):
        self.mkg("TASK-49", "publish", git=None)
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(
            self.prune(gql), (0, "prune-skip entry=TASK-49/publish reason=not a clone or worktree")
        )
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(len(logged(self.logs)), 1)

    def test_clones_deleted_beside_skipped_entries(self):
        clone, bare = (
            self.mkc("TASK-49", "src", "o", "repo"),
            self.mkg("TASK-49", "src", "o", "bare", git=None),
        )
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                "prune-skip entry=TASK-49/src/o/bare reason=not a clone or worktree",
                "prune-removed entry=TASK-49/src/o/repo what=clone",
            ],
        )
        self.assertFalse(os.path.lexists(clone))
        self.assertTrue(os.path.isdir(bare))

    def test_young_and_in_progress_untouched(self):
        path = self.mkc("TASK-48", "src", "o", "repo")
        busy = self.mkc("TASK-50", "src", "o", "repo")
        self.api.live = ["engineer-TASK-48-aaaaaaaa", "engineer-TASK-50-bbbbbbbb"]
        gql = gql_for(
            {
                "TASK-48": ("Done", [(50, "Done"), (30, "In Progress"), (24 - 1 / 3600, "Done")]),
                "TASK-50": ("In Progress", [(50, "Done"), (30, "In Progress")]),
            }
        )
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertTrue(os.path.isdir(path) and os.path.isdir(busy))
        self.assertEqual(self.api.order, [])

    def test_unknown_finish_time_skipped(self):
        path = self.mkc("TASK-49", "src", "o", "repo")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [])}))
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49 reason=finish time unknown", out)
        self.assertTrue(os.path.isdir(path))

    def test_no_entry_no_candidate(self):
        def publish_file():
            os.makedirs(os.path.join(self.work, "TASK-49"))
            write(os.path.join(self.work, "TASK-49", "publish"), "x")

        for name, make in (
            ("empty src", lambda: os.makedirs(os.path.join(self.work, "TASK-49", "src"))),
            (
                "empty owner dir",
                lambda: os.makedirs(os.path.join(self.work, "TASK-49", "src", "o")),
            ),
            ("publish a file", publish_file),
        ):
            with self.subTest(name):
                self.fresh()
                make()
                gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
                self.assertEqual(self.prune(gql), (0, ""))
                self.assertNotIn(prune.Q_ISSUE, gql.calls)

    def test_core_clones_in_src_and_publish_deleted_after_24h(self):
        src, publish = self.mkc("TASK-49", "src", "o", "repo"), self.mkc("TASK-49", "publish")
        busy = self.mkc("TASK-50", "src", "o", "repo")
        gql = gql_for(
            {
                "TASK-49": ("Done", [(30, "In Progress"), (24, "Done")]),
                "TASK-50": ("In Progress", [(30, "In Progress")]),
            }
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                "prune-removed entry=TASK-49/src/o/repo what=clone",
                "prune-removed entry=TASK-49/publish what=clone",
            ],
        )
        self.assertFalse(os.path.lexists(src))
        self.assertFalse(os.path.lexists(publish))
        self.assertTrue(os.path.isdir(os.path.join(self.work, "TASK-49")))
        self.assertTrue(os.path.isdir(busy))
        self.assertOutsideKept()
        self.assertNotIn(ops.Q_TEAM, gql.calls)
        self.assertIn("state { id }", prune.Q_ISSUE)

    def test_dry_run_plans_clone_and_worktree_deletion_and_touches_nothing(self):
        clones = [self.mkc("TASK-49", "src", "o", "m"), self.mkc("TASK-49", "publish")]
        wt = self.mkg("TASK-49", "src", "o", "n")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
        code, out = self.prune(gql, dry=True)
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                "prune-plan entry=TASK-49/src/o/m what=delete the clone",
                "prune-plan entry=TASK-49/src/o/n what=delete the worktree",
                "prune-plan entry=TASK-49/publish what=delete the clone",
                "prune-plan TASK-49 what=archive",
            ],
        )
        self.assertNotIn(prune.M_ARCHIVE, gql.calls)
        self.assertTrue(all(os.path.isdir(os.path.join(p, ".git")) for p in clones))
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))

    def test_symlinked_clone_entries_refused(self):
        victim = self.mkc("TASK-50", "src", "o", "repo")
        os.makedirs(os.path.join(self.work, "TASK-49", "src"))
        os.symlink(victim, os.path.join(self.work, "TASK-49", "src", "link"))
        os.symlink(victim, os.path.join(self.work, "TASK-49", "publish"))
        write(os.path.join(self.work, "TASK-49", "src", "file"), "x")
        os.makedirs(os.path.join(self.work, "TASK-48"))
        os.symlink(
            os.path.dirname(os.path.dirname(victim)), os.path.join(self.work, "TASK-48", "src")
        )
        done = ("Done", [(30, "Done")])
        code, out = self.prune(
            gql_for({"TASK-48": done, "TASK-49": done, "TASK-50": ("In Progress", [])})
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                (
                    "prune-skip entry=TASK-48/src/o/repo reason=not a real directory inside TASK-48/src/o/, refusing to touch"
                ),
                (
                    "prune-skip entry=TASK-49/src/file reason=not a real directory inside TASK-49/src/, refusing to touch"
                ),
                (
                    "prune-skip entry=TASK-49/src/link reason=not a real directory inside TASK-49/src/, refusing to touch"
                ),
                (
                    "prune-skip entry=TASK-49/publish reason=not a real directory inside TASK-49/, refusing to touch"
                ),
            ],
        )
        self.assertTrue(os.path.isdir(os.path.join(victim, ".git")))

    def test_clone_deletion_failure_logs_the_error_and_exit_3(self):
        src, publish = self.mkc("TASK-49", "src", "o", "repo"), self.mkc("TASK-49", "publish")
        with mock.patch.object(
            shutil, "rmtree", side_effect=[PermissionError("denied\nby os"), None]
        ) as rmtree:
            code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 3)
        self.assertEqual([c.args for c in rmtree.call_args_list], [(src,), (publish,)])
        self.assertEqual(
            out.splitlines(),
            [
                "prune-error entry=TASK-49/src/o/repo error=rmtree: PermissionError: denied by os",
                "prune-removed entry=TASK-49/publish what=clone",
            ],
        )

    def test_worktrees_deleted_like_clones_with_uncommitted_work(self):
        a, b = (self.mkg("TASK-49", "src", "o", n) for n in "ab")
        e, gh, pub = (
            self.mkg("TASK-49", "src", "p", "e"),
            self.mkg("TASK-49", "src", "q", ".github"),
            self.mkg("TASK-49", "publish"),
        )
        write(os.path.join(a, "notes.md"), "uncommitted")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                "prune-removed entry=TASK-49/src/o/a what=worktree",
                "prune-removed entry=TASK-49/src/o/b what=worktree",
                "prune-removed entry=TASK-49/src/p/e what=worktree",
                "prune-removed entry=TASK-49/src/q/.github what=worktree",
                "prune-removed entry=TASK-49/publish what=worktree",
            ],
        )
        self.assertFalse(any(os.path.lexists(p) for p in (a, b, e, gh, pub)))
        self.assertFalse(
            any(os.path.lexists(os.path.join(self.work, "TASK-49", "src", o)) for o in "opq")
        )
        self.assertTrue(os.path.isdir(os.path.join(self.work, "TASK-49")))

    def test_symlinked_owner_dir_and_owner_entries_refused(self):
        victim = self.mkg("TASK-50", "src", "o", "n")
        os.makedirs(os.path.join(self.work, "TASK-49", "src"))
        os.symlink(os.path.dirname(victim), os.path.join(self.work, "TASK-49", "src", "o"))
        os.makedirs(os.path.join(self.work, "TASK-48", "src", "o"))
        os.symlink(victim, os.path.join(self.work, "TASK-48", "src", "o", "n"))
        write(os.path.join(self.work, "TASK-48", "src", "o", "file"), "x")
        done = ("Done", [(30, "Done")])
        code, out = self.prune(
            gql_for({"TASK-48": done, "TASK-49": done, "TASK-50": ("In Progress", [])})
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                (
                    "prune-skip entry=TASK-48/src/o/file reason=not a real directory inside TASK-48/src/o/, refusing to touch"
                ),
                (
                    "prune-skip entry=TASK-48/src/o/n reason=not a real directory inside TASK-48/src/o/, refusing to touch"
                ),
                (
                    "prune-skip entry=TASK-49/src/o reason=not a real directory inside TASK-49/src/, refusing to touch"
                ),
            ],
        )
        self.assertTrue(os.path.isfile(os.path.join(victim, ".git")))
        self.assertTrue(os.path.islink(os.path.join(self.work, "TASK-49", "src", "o")))

    def test_emptied_issue_stops_being_a_candidate(self):
        self.mkg("TASK-49", "src", "o", "n")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql)[0], 0)
        self.assertFalse(os.path.lexists(os.path.join(self.work, "TASK-49", "src", "o")))
        again = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(again), (0, ""))
        self.assertNotIn(prune.Q_ISSUE, again.calls)

    def test_tmp_goes_and_the_rest_of_the_issues_dir_and_logs_stay(self):
        ident = os.path.join(self.work, "TASK-49")
        os.makedirs(os.path.join(ident, "tmp", "deep"))
        write(os.path.join(ident, "tmp", "deep", "pr.md"), "x")
        os.chmod(os.path.join(ident, "tmp", "deep", "pr.md"), 0o444)
        kept = [os.path.join(ident, f) for f in ("run.jsonl", "writeback.json", "out.md")]
        for f in kept:
            write(f, "x")
        os.makedirs(self.logs)
        write(os.path.join(self.logs, "runs.jsonl"), "x")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual(
            (code, out.splitlines()), (0, ["prune-removed entry=TASK-49/tmp what=temp files"])
        )
        self.assertFalse(os.path.lexists(os.path.join(ident, "tmp")))
        self.assertTrue(
            all(os.path.isfile(f) for f in kept + [os.path.join(self.logs, "runs.jsonl")])
        )

    def test_tmp_dry_run_and_a_symlinked_tmp(self):
        tmp = os.path.join(self.work, "TASK-49", "tmp")
        os.makedirs(tmp)
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}), dry=True)
        self.assertEqual(
            (code, out.splitlines()),
            (0, ["prune-plan entry=TASK-49/tmp what=delete the temp files"]),
        )
        self.assertTrue(os.path.isdir(tmp))
        os.rmdir(tmp)
        os.symlink(self.outside, tmp)
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual(
            (code, out.splitlines()),
            (
                0,
                [
                    (
                        "prune-skip entry=TASK-49/tmp reason=not a real directory inside TASK-49/, refusing to touch"
                    )
                ],
            ),
        )
        self.assertOutsideKept()

    def test_dot_files_under_an_owner_are_no_entries(self):
        wt = self.mkg("TASK-49", "src", "o", "n")
        write(os.path.join(self.work, "TASK-49", "src", "o", ".DS_Store"), "x")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual(
            (code, out.splitlines()), (0, ["prune-removed entry=TASK-49/src/o/n what=worktree"])
        )
        self.assertFalse(os.path.lexists(wt))


class RetentionCleanup(PruneScenario):
    def test_writable_dirs_default_to_work_and_the_temp_dirs(self):
        pruner = prune.Pruner(
            None,
            NOW,
            dry=False,
            work=self.work,
            cfg=self.cfg,
            team=TEAM_OBJ,
            roles=ROLES,
        )
        self.assertEqual(pruner.writable, (self.work, *temp_dirs()))

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

    def test_recorded_run_alone_closes_all_owned_sessions_when_finished(self):
        self.record("TASK-7", SID, [B, A])
        self.record("TASK-8", OTHER_SID, ["other-host-agent"])
        gql = gql_for(
            {"TASK-7": ("Done", [(24, "Done")]), "TASK-8": ("In Progress", [(30, "In Progress")])}
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [f"prune-closed TASK-7 session={SID}"])
        self.assertEqual(gql.calls.count(prune.Q_ISSUE), 2)
        self.assertEqual(self.closed(), [B, A])
        again = gql_for({"TASK-7": ("Done", [(24, "Done")]), "TASK-8": ("In Progress", [])})
        self.prune(again)
        self.assertEqual(self.closed(), [B, A])

    def test_unrecorded_matching_names_never_make_candidates(self):
        self.api.live = [A, "agent-pm-engineer-TASK-7", "notes"]
        gql = gql_for({"TASK-7": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(self.closed(), [])
        self.assertNotIn(prune.Q_ISSUE, gql.calls)

    def test_only_recorded_configured_roles_make_candidates(self):
        self.record("TASK-7", SID, [A])
        self.record("TASK-9", OTHER_SID, ["designer-agent"], role="designer")
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-9"), ("Done", [(30, "Done")])))
        self.assertEqual(self.prune(gql), (0, f"prune-closed TASK-7 session={SID}"))
        self.assertEqual(self.closed(), [A])
        self.assertEqual(gql.calls.count(prune.Q_ISSUE), 1)

    def test_clone_and_session_issue_queried_once_sessions_first(self):
        clone = self.mkc("TASK-7", "src", "o", "repo")
        self.record("TASK-7", SID, [A])
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
                f"prune-closed TASK-7 session={SID}",
                "prune-removed entry=TASK-7/src/o/repo what=clone",
            ],
        )
        self.assertEqual((seen, self.closed()), ([True], [A]))
        self.assertEqual(gql.calls.count(prune.Q_ISSUE), 1)
        self.assertFalse(os.path.lexists(clone))

    def test_dry_run_plans_recorded_run_and_closes_none(self):
        self.record("TASK-7", SID, [B, A])
        self.record("TASK-70", OTHER_SID, ["other-host-agent"])
        code, out = self.prune(
            gql_for({"TASK-7": ("Done", [(30, "Done")]), "TASK-70": ("In Progress", [])}),
            dry=True,
        )
        self.assertEqual(
            (code, out),
            (0, f"prune-plan TASK-7 session={SID} what=close the run and forget its state"),
        )
        self.assertEqual(self.api.order, [])
        PruneDelegate.attach(self.cfg.run_dir("TASK-7"))

    def test_a_closed_finished_run_is_forgotten_and_its_workdir_kept(self):
        self.record("TASK-7", SID, [A])
        code, out = self.prune(gql_for({"TASK-7": ("Done", [(30, "Done")])}))
        self.assertEqual((code, out), (0, f"prune-closed TASK-7 session={SID}"))
        with self.assertRaises(ConfigError):
            PruneDelegate.attach(self.cfg.run_dir("TASK-7"))
        self.assertTrue(os.path.isdir(self.cfg.run_dir("TASK-7")))

    def test_a_failed_close_or_an_unfinished_issue_keeps_the_runs_state(self):
        self.record("TASK-7", SID, [A])
        self.record("TASK-8", OTHER_SID, [B])
        self.api.errors = {A: SessionError("boom")}
        gql = gql_for({"TASK-7": ("Done", [(30, "Done")]), "TASK-8": ("Done", [(2, "Done")])})
        self.assertEqual(self.prune(gql)[0], 3)
        for ident in ("TASK-7", "TASK-8"):
            with self.subTest(ident=ident):
                PruneDelegate.attach(self.cfg.run_dir(ident))

    def test_a_failed_forget_is_counted_after_the_close(self):
        self.record("TASK-7", SID, [A])
        with mock.patch.object(PruneDelegate, "forget", side_effect=ConfigError("run is running")):
            code, out = self.prune(gql_for({"TASK-7": ("Done", [(30, "Done")])}))
        self.assertEqual(
            (code, out.splitlines()),
            (
                3,
                [
                    f"prune-closed TASK-7 session={SID}",
                    f"prune-error TASK-7 session={SID} error=ConfigError: run is running",
                ],
            ),
        )

    def test_sessions_unknown_prune_no_issue_but_archive(self):
        for dry, archived in (
            (False, "prune-archived TASK-49"),
            (True, "prune-plan TASK-49 what=archive"),
        ):
            with self.subTest(dry=dry):
                self.fresh()
                self.api.order.clear()
                kept = [self.mkc("TASK-49", "src", "o", "repo"), self.mkc("TASK-49", "publish")]
                self.record("TASK-49", SID, [A])
                self.unknown = True
                gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
                code, out = self.prune(gql, dry=dry)
                self.assertEqual(
                    (code, out.splitlines()),
                    (3, ["prune-error error=run inventory unknown", archived]),
                )
                self.assertTrue(all(os.path.isdir(os.path.join(p, ".git")) for p in kept))
                self.assertNotIn(prune.Q_ISSUE, gql.calls)
                self.assertEqual(gql.archived, [] if dry else ["id-TASK-49"])
                self.assertEqual(self.api.order, [])

    def test_close_failure_counted(self):
        for error in (SessionError("boom"), ConfigError("bad name")):
            with self.subTest(error=type(error).__name__):
                self.api.order.clear()
                self.record("TASK-7", SID, [B, A])
                self.api.errors = {B: error}
                code, out = self.prune(gql_for({"TASK-7": ("Done", [(30, "Done")])}))
                self.assertEqual(code, 3)
                self.assertEqual(
                    out.splitlines(),
                    [
                        f"prune-error TASK-7 session={SID} error={type(error).__name__}: {error}",
                    ],
                )
                self.assertEqual(self.closed(), [B, A])

    def test_close_failure_still_closes_other_runs_and_deletes_workspace(self):
        clone = self.mkc("TASK-7", "src", "o", "repo")
        self.record("TASK-7", SID, [B, A])
        self.api.errors = {B: SessionError("boom")}
        code, out = self.prune(gql_for({"TASK-7": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 3)
        self.assertEqual(self.closed(), [B, A])
        self.assertFalse(os.path.exists(clone))
        self.assertEqual(
            out.splitlines(),
            [
                f"prune-error TASK-7 session={SID} error=SessionError: boom",
                "prune-removed entry=TASK-7/src/o/repo what=clone",
            ],
        )

    def test_failed_run_close_does_not_stop_another_finished_issue(self):
        self.record("TASK-7", SID, [A])
        self.record("TASK-8", OTHER_SID, ["other-host-agent"])
        self.api.errors = {A: SessionError("boom")}
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(30, "Done")])))
        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertEqual(self.closed(), [A, "other-host-agent"])
        self.assertEqual(
            out.splitlines(),
            [
                f"prune-error TASK-7 session={SID} error=SessionError: boom",
                f"prune-closed TASK-8 session={OTHER_SID}",
            ],
        )

    def test_a_run_without_delegate_state_skips_once_and_other_issues_still_prune(self):
        clone = self.mkc("TASK-7", "src", "o", "repo")
        self.record("TASK-7", SID, [A])
        self.record("TASK-8", OTHER_SID, ["other-host-agent"])
        os.unlink(os.path.join(self.cfg.run_dir("TASK-7"), "delegate.json"))
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(30, "Done")])))
        skip = (
            f"prune-skip TASK-7 sid={SID} reason=ConfigError: "
            f"{self.cfg.run_dir('TASK-7')!r}: no delegate.json"
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(self.closed(), ["other-host-agent"])
        self.assertFalse(os.path.exists(clone))
        self.assertIn(skip, out.splitlines())
        self.assertIn(f"prune-closed TASK-8 session={OTHER_SID}", out.splitlines())
        self.assertNotIn(skip, self.prune(gql)[1].splitlines())

    def test_promote_tick_archives_with_its_roles(self):
        gql = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        code, _, out = self.tick(gql)
        self.assertEqual((code, gql.archived), (0, ["id-TASK-1"]))
        self.assertIn("prune-archived TASK-1", out)
        self.assertEqual(self.user_queries, len(tp.ROLE))

    def test_promote_tick_closes_recorded_runs_through_the_delegate(self):
        self.record("TASK-7", SID, [A])
        code, _, out = self.tick(gql_for({"TASK-7": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 0)
        self.assertIn(f"prune-closed TASK-7 session={SID}", out)
        self.assertEqual(self.api.order, [("close", A)])

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


class PruneCommand(PruneScenario):
    """agent-pm-linear prune: one prune tick alone, as promote's tick runs it."""

    def command(self, prune_gql, *argv):
        write(
            os.path.join(self.agent_pm, "config.local.toml"),
            f'[linear_orchestrator]\nwork_dir = "{self.root}"\n' + CORE_LOCAL,
        )
        path = os.path.join(self.root, "config.toml")
        write(path, tp.CONFIG)
        fake = tp.FakeLinear()

        def gql(query, **v):
            board = query not in (prune.Q_ISSUE, prune.Q_FINISHED, prune.M_ARCHIVE)
            return (fake if board else prune_gql)(query, **v)

        before, out, err = len(logged(self.logs)), io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = prune.main(list(argv), gql=gql, now=NOW, config_path=path, pruner=self.pruner)
        lines = err.getvalue().splitlines()
        copies = [json.loads(line) for line in lines if line.startswith("{")]
        said = "\n".join(show(e) for e in logged(self.logs)[before:] + copies)
        rest = "".join(f"{line}\n" for line in lines if not line.startswith("{"))
        return code, out.getvalue(), rest, said

    def test_a_tick_deletes_finished_checkouts_and_archives(self):
        clone = self.mkc("TASK-49", "src", "o", "repo")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
        self.assertEqual(self.command(gql)[:3], (0, "", ""))
        self.assertFalse(os.path.exists(clone))
        self.assertEqual(gql.archived, ["id-TASK-49"])
        self.assertOutsideKept()

    def test_a_dry_run_plans_and_changes_nothing(self):
        clone = self.mkc("TASK-49", "src", "o", "repo")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
        code, out, err, said = self.command(gql, "--dry-run")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertTrue(os.path.isdir(os.path.join(clone, ".git")))
        self.assertEqual(gql.archived, [])
        self.assertEqual(
            said.splitlines(),
            [
                "prune-plan entry=TASK-49/src/o/repo what=delete the clone",
                "prune-plan TASK-49 what=archive",
            ],
        )

    def test_a_failed_step_exits_1_after_the_rest(self):
        def down(query, **v):
            if query == prune.Q_ISSUE:
                raise LinearError("linear api error: down")
            return gql_for({})(query, **v)

        self.mkc("TASK-49", "src", "o", "repo")
        code, _, _, said = self.command(down)
        self.assertEqual(code, 1)
        self.assertIn("prune-error TASK-49 error=Linear: LinearError: linear api error: down", said)

    def test_other_arguments_are_a_usage_error(self):
        for argv in (["--now"], ["TASK-1"], ["--dry-run", "x"]):
            with self.subTest(argv=argv):
                code, out, err, said = self.command(gql_for({}), *argv)
                self.assertEqual((code, out, said), (2, "", ""))
                self.assertIn("agent-pm-linear prune", err)


HARNESS_DELEGATE = load_class("agent_pm.delegate.engine.harness_delegate:HarnessDelegate", Delegate)


class StateDir(HarnessCase):
    """FR-26: a pruned run's real HarnessDelegate state leaves the State dir."""

    def test_pruning_a_finished_run_deletes_its_state_dir_files_and_keeps_its_workdir(self):
        cfg = config.Config(
            team="",
            states={},
            human_members=(),
            harness_key="",
            task_label_group="",
            task_labels={},
            roles={},
            project_repos={},
            local_clones={},
            work_dir=os.path.join(self.home, "pipeline"),
            docs=Docs("", "", {}),
            stages={},
            delegate_kind="harness",
            delegates={"harness": HARNESS_DELEGATE},
            core={},
        )
        event_log.use_logs(cfg.logs_dir)
        self.work = cfg.run_dir("TASK-7")
        spec = Spec("Echo input.", self.work, task="echo", sid=SID, cwd=self.work, client="fake")
        HARNESS_DELEGATE(
            "dummy-tester",
            spec,
            session_cls=HarnessSession,
            host="agent",
            harness_host="harness",
        ).detach()
        runs.note(
            cfg.runs_log,
            "start",
            issue="TASK-7",
            sid=SID,
            role="dummy-tester",
            client="fake",
            delegate_kind="harness",
        )
        state = os.path.join(self.agent_pm, "delegates")
        self.assertTrue(os.listdir(state))
        code = prune.Pruner(
            gql_for({"TASK-7": ("Done", [(30, "Done")])}),
            NOW,
            dry=False,
            run=Git(),
            work=cfg.runs_dir,
            cfg=cfg,
            team=TEAM_OBJ,
            roles=ROLES,
            writable=(cfg.runs_dir,),
        ).run()
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(state), [])
        self.assertTrue(os.path.isdir(self.work))


class CloneConfiguration(Case):
    def test_clones_are_configs(self):
        self.assertIs(prune.CLONES, config.CLONES)
