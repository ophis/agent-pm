"""Worktree behavior."""

import io
import json
import os
import shutil
import unittest
import unittest.mock

from agent_pm.delegate.team.tools.types import RepoServices

from delegate.tests.unit.tools.repo.case import FETCH, HEAD, SHA, Base, Fake, fail, info, ok, repo


class Worktree(Base):
    def worktree(self, *answers, branch="TASK-1-x", spec="o/n", config=None, argv=()):
        run = Fake([*answers, HEAD])
        return self.main(
            ["worktree", spec, f"--branch={branch}", "--dir", self.dir, *argv], run, config
        ), run

    def test_new_clone_branch_starts_from_the_explicit_base(self):
        (code, _, err), run = self.worktree(info(), argv=["--base", "re-architecture"])
        self.assertEqual(code, 0, err)
        self.assertTrue(
            run.ran(
                "git",
                "-C",
                self.wt,
                "checkout",
                "--no-track",
                "-b",
                "TASK-1-x",
                "origin/re-architecture",
            )
        )

    def test_explicit_base_preserves_existing_clone_branch_precedence(self):
        for local in (True, False):
            with self.subTest(local=local):
                (code, _, err), run = self.worktree(
                    self.existing(),
                    info(),
                    (lambda a: a[3:5] == ["branch", "--list"], ok("TASK-1-x" if local else "")),
                    (lambda a: "ls-remote" in a, ok("abc\trefs/heads/TASK-1-x\n")),
                    argv=["--base", "missing"],
                )
                self.assertEqual(code, 0, err)
                options = [] if local else ["--track", "-b", "TASK-1-x"]
                start = "TASK-1-x" if local else "origin/TASK-1-x"
                self.assertTrue(run.ran("git", "-C", self.wt, "checkout", *options, start))
                self.assertEqual(run.ran("git", "-C", self.wt, "ls-remote"), not local)

    def test_unsafe_base_is_refused_before_fetch_or_clone(self):
        for base in ("-x", "a..b", "x;rm -rf ~", "", "x/", "a" * 101):
            with self.subTest(base=base):
                (code, _, err), run = self.worktree(info(), argv=[f"--base={base}"])
                self.assertEqual(code, 2, err)
                self.assertIn("unsafe base branch name", err)
                self.assertFalse(
                    any("fetch" in c or "clone" in c or "checkout" in c for c in run.calls)
                )

    def test_missing_clone_base_reports_the_git_failure(self):
        (code, _, err), _ = self.worktree(
            info(),
            (lambda a: "checkout" in a, fail("fatal: 'origin/missing' is not a commit")),
            argv=["--base", "missing"],
        )
        self.assertEqual(code, 1, err)
        self.assertIn("origin/missing", err)

    def test_fresh_blobless_clone_on_a_new_branch_from_the_default(self):
        (code, out, _), run = self.worktree(info(default="trunk"))
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out),
            {
                "repo": "o/n",
                "host": "github.com",
                "default": "trunk",
                "branch": "TASK-1-x",
                "commit": SHA,
                "worktree": self.wt,
                "permalink_base": f"https://github.com/o/n/blob/{SHA}/",
                "push": True,
            },
        )
        self.assertIn(
            [
                "gh",
                "repo",
                "clone",
                "github.com/o/n",
                self.wt,
                "--",
                "-c",
                "core.symlinks=false",
                "--filter=blob:none",
            ],
            run.calls,
        )
        self.assertTrue(
            run.ran(
                "git", "-C", self.wt, "checkout", "--no-track", "-b", "TASK-1-x", "origin/trunk"
            )
        )
        self.assertEqual(run.timeout("git", "-C", self.wt, "checkout"), repo.LONG)

    def test_a_remote_repo_gets_no_symlinks_whatever_the_list(self):
        (code, _, err), run = self.worktree(
            info(), config=self.config(f'trusted_dirs = ["{self.dir}", "~/n"]\n')
        )
        self.assertEqual(code, 0, err)
        self.assertIn(
            [
                "gh",
                "repo",
                "clone",
                "github.com/o/n",
                self.wt,
                "--",
                "-c",
                "core.symlinks=false",
                "--filter=blob:none",
            ],
            run.calls,
        )

    def test_a_bad_trusted_dirs_fails_worktree_before_the_repo_is_read(self):
        for text, want in (
            ("trusted_dirs = [\n", "(at "),
            ('trusted_dirs = "/a"\n', "trusted_dirs"),
            ("trusted_dirs = [1]\n", "trusted_dirs"),
            ('trusted_dirs = ["a/b"]\n', "trusted_dirs"),
            ('trusted_dirs = ["~nosuchuser/x"]\n', "trusted_dirs"),
        ):
            with self.subTest(text=text):
                run = Fake([])
                code, _, err = self.main(
                    ["worktree", "--dir", self.dir, "--branch", "TASK-1-x", "not a repo"],
                    run,
                    self.config(text),
                )
                self.assertEqual((code, run.calls), (1, []), err)
                self.assertIn(want, err)

    def test_remote_branch_is_tracked(self):
        (code, _, _), run = self.worktree(
            info(), (lambda a: "ls-remote" in a, ok("abc\trefs/heads/TASK-1-x\n"))
        )
        self.assertEqual(code, 0)
        self.assertTrue(
            run.ran("git", "-C", self.wt, "ls-remote", "--heads", "origin", "refs/heads/TASK-1-x")
        )
        self.assertTrue(
            run.ran(
                "git", "-C", self.wt, "checkout", "--track", "-b", "TASK-1-x", "origin/TASK-1-x"
            )
        )
        self.assertEqual(run.timeout("git", "-C", self.wt, "checkout"), repo.LONG)

    def test_default_branch_is_invalid(self):
        (code, _, err), run = self.worktree(info(default="main"), branch="main")
        self.assertEqual(code, 2)
        self.assertIn("default branch", err)
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_no_push_permission_is_reported_not_refused(self):
        (code, out, _), run = self.worktree(info(push=False))
        self.assertEqual((code, json.loads(out)["push"]), (0, False))
        self.assertTrue(run.ran("gh", "repo", "clone"))

    def test_reuse_fetches_and_switches_to_the_local_branch(self):
        (code, _, _), run = self.worktree(
            self.existing(), info(), (lambda a: a[3:5] == ["branch", "--list"], ok("  TASK-1-x\n"))
        )
        self.assertEqual(code, 0)
        self.assertTrue(run.ran("git", "-C", self.wt, *FETCH))
        self.assertTrue(run.ran("git", "-C", self.wt, "checkout", "TASK-1-x"))
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_already_on_the_branch_changes_nothing(self):
        (code, _, _), run = self.worktree(
            self.existing(),
            info(),
            (lambda a: "--show-current" in a, ok("TASK-1-x\n")),
            argv=["--base", "re-architecture"],
        )
        self.assertEqual(code, 0)
        self.assertFalse(any("checkout" in c or "worktree" in c for c in run.calls))

    def test_the_checkout_is_named_by_owner_and_name_else_by_the_branch(self):
        """One --dir: each row's checkout lands beside the earlier ones (the same name under two
        owners included).
        """
        for spec, branch, argv, path in (
            ("a/n", "TASK-1-x", [], "a/n-TASK-1-x"),
            ("b/n", "TASK-1-x", [], "b/n-TASK-1-x"),
            ("o/n", "feat/x", [], "o/n-feat-x"),
            ("o/n", "feat/x", ["--name", "TASK-1"], "o/n-TASK-1"),
        ):
            with self.subTest(spec=spec, argv=argv):
                run = Fake([info(), HEAD])
                code, out, err = self.main(
                    ["worktree", spec, "--branch", branch, *argv, "--dir", self.dir], run
                )
                wt = os.path.join(self.dir, path)
                self.assertEqual((code, json.loads(out)["worktree"]), (0, wt), err)
                self.assertTrue(run.ran("gh", "repo", "clone", f"github.com/{spec}", wt))

    def test_a_bad_name_is_refused_before_the_clone_is_read(self):
        clone = os.path.join(self.dir, "clone")
        os.makedirs(os.path.join(clone, ".git"))
        for cmd in ("worktree", "status"):
            for spec in ("o/n", clone):
                for name in ("", "a/b", "../x", "a b", "x;y", "~", "x" * 101):
                    with self.subTest(cmd=cmd, spec=spec, name=name):
                        run = Fake([])
                        code, _, err = self.main(
                            [cmd, spec, "--branch", "TASK-1-x", "--name", name, "--dir", self.dir],
                            run,
                        )
                        self.assertEqual((code, run.calls), (2, []))
                        self.assertIn("unsafe checkout name", err)
        self.assertEqual(os.listdir(self.dir), ["clone"])

    def test_anything_else_in_the_way_is_invalid(self):
        def other_repo():
            return [self.existing("https://github.com/x/n")]

        def a_worktree_for_a_remote_repo():
            os.makedirs(self.wt)
            with open(os.path.join(self.wt, ".git"), "w") as f:
                f.write("gitdir: /elsewhere/.git/worktrees/n\n")
            return []

        def a_symlink():
            os.makedirs(os.path.dirname(self.wt))
            os.symlink(self.dir, self.wt)
            return []

        for setup, want in (
            (other_repo, "not a checkout of github.com/o/n"),
            (a_worktree_for_a_remote_repo, "not a checkout of github.com/o/n"),
            (a_symlink, "exists and is not a checkout"),
        ):
            with self.subTest(setup.__name__):
                (code, _, err), run = self.worktree(*setup(), info())
                self.assertEqual(code, 2, err)
                self.assertIn(want, err)
                self.assertFalse(run.ran("gh", "repo", "clone"))
                shutil.rmtree(os.path.join(self.dir, "o"))

    def test_not_found_is_invalid(self):
        (code, _, err), run = self.worktree((["gh", "api"], fail("gh: Not Found (HTTP 404)")))
        self.assertEqual(code, 2)
        self.assertIn("HTTP 404", err)
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_a_token_in_git_output_is_redacted(self):
        token = fail("fatal: could not read Password for 'https://TOK@github.com'")
        (code, _, err), _ = self.worktree(self.existing(), info(), (lambda a: "fetch" in a, token))
        self.assertEqual(code, 1)
        self.assertNotIn("TOK", err)
        self.assertIn("https://***@github.com", err)

    def test_network_failure_exits_1(self):
        (code, _, err), _ = self.worktree(
            info(), (["gh", "repo", "clone"], fail("connection reset"))
        )
        self.assertEqual(code, 1)
        self.assertIn("connection reset", err)

    def test_unreadable_repo_is_invalid(self):
        (code, _, err), run = self.worktree(info(), spec="not a repo")
        self.assertEqual(code, 2)
        self.assertIn("unreadable repo", err)
        self.assertEqual(run.calls, [])

    def test_a_local_path_whose_origin_names_no_repo_is_invalid(self):
        clone = os.path.join(self.dir, "clone")
        os.makedirs(os.path.join(clone, ".git"))
        (code, _, err), run = self.worktree(
            (lambda a: a[3:] == ["remote", "get-url", "origin"], ok("/srv/n.git\n")), spec=clone
        )
        self.assertEqual(code, 2)
        self.assertIn("no origin URL naming host/owner/name", err)
        self.assertFalse(run.ran("gh"))

    def test_an_unsafe_branch_is_invalid_before_the_repo_is_read(self):
        for branch in ("-x", "a..b", "a b", "x/"):
            with self.subTest(branch=branch):
                (code, _, err), run = self.worktree(info(), branch=branch, spec="not a repo")
                self.assertEqual((code, run.calls), (2, []), err)
                self.assertIn("unsafe branch name", err)

    def refused(self, argv):
        """argparse's stderr for `argv`, which it must refuse."""
        err = io.StringIO()
        with self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr", err):
            repo.main(argv, config=self.none, local=self.none, services=RepoServices(run=Fake([])))
        return err.getvalue()

    def test_an_option_given_twice_is_refused(self):
        rows = [
            (cmd, argv, opt)
            for cmd in ("worktree", "status")
            for argv, opt in (
                (
                    [
                        "--dir",
                        self.dir,
                        "--branch",
                        "b",
                        "o/n",
                        "--dir",
                        os.path.expanduser("~/.claude"),
                    ],
                    "--dir",
                ),
                (["--dir", self.dir, "--branch", "b", "o/n", "--branch", "c"], "--branch"),
                (
                    ["--dir", self.dir, "--branch", "b", "--name", "x", "o/n", "--name", "y"],
                    "--name",
                ),
                (
                    ["--dir", self.dir, "--branch", "b", "--base", "a", "--base", "b", "o/n"],
                    "--base",
                ),
            )
        ]
        for cmd, argv, opt in rows:
            with self.subTest(cmd=cmd, opt=opt):
                self.assertIn(f"{opt} given twice", self.refused([cmd, *argv]))

    def test_a_bad_command_line_is_refused(self):
        """--branch is required; only worktree and status exist (prepare and checkout are gone)."""
        for argv, want in (
            (["worktree", "--dir", self.dir, "o/n"], "--branch"),
            (["status", "--dir", self.dir, "o/n"], "--branch"),
            (["prepare", "--dir", self.dir, "--branch", "b", "o/n"], "prepare"),
            (["checkout", "--dir", self.dir, "--branch", "b", "o/n"], "checkout"),
        ):
            with self.subTest(argv=argv):
                self.assertIn(want, self.refused(argv))
