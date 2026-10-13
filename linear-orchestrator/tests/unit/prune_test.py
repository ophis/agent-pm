import os
import shutil
from functools import partial
from unittest import mock

from agent_pm.linear_orchestrator.engine import prune
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.prune.data import gql_for, write
from orchestrator_test_helpers.unit.prune.prune_test_fixtures import PruneTest as PruneTestBase
from orchestrator_test_helpers.unit.run_fixtures import logged


class PruneTest1(PruneTestBase):
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
        self.assertEqual(self.api.order, [("sessions", None)])

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
        self.assertNotIn(linear.Q_TEAM, gql.calls)
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
                "prune-skip entry=TASK-48/src/o/repo reason=not a real directory inside TASK-48/src/o/, refusing to touch",
                "prune-skip entry=TASK-49/src/file reason=not a real directory inside TASK-49/src/, refusing to touch",
                "prune-skip entry=TASK-49/src/link reason=not a real directory inside TASK-49/src/, refusing to touch",
                "prune-skip entry=TASK-49/publish reason=not a real directory inside TASK-49/, refusing to touch",
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
                "prune-skip entry=TASK-48/src/o/file reason=not a real directory inside TASK-48/src/o/, refusing to touch",
                "prune-skip entry=TASK-48/src/o/n reason=not a real directory inside TASK-48/src/o/, refusing to touch",
                "prune-skip entry=TASK-49/src/o reason=not a real directory inside TASK-49/src/, refusing to touch",
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
                    "prune-skip entry=TASK-49/tmp reason=not a real directory inside TASK-49/, refusing to touch"
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
