"""Local checkout behavior."""

import io
import json
import os
import threading
import unittest
import unittest.mock

from agent_pm.delegate.team.tools.types import CheckoutRequest, RepoServices

from delegate.tests.unit.tools.repo.case import (
    FETCH,
    LOGIN,
    Clone,
    Real,
    git,
    info,
    pr_list,
    repo,
    upstream,
)


class Local(Clone):
    def worktree(self, branch="TASK-1-x", spec=None, base=None, config=None, base_branch=None):
        run, out, err = Real([info()]), io.StringIO(), io.StringIO()
        code = repo.main(
            [
                "worktree",
                "--dir",
                base or self.dir,
                "--branch",
                branch,
                *([] if base_branch is None else ["--base", base_branch]),
                spec or self.clone,
            ],
            config=config or self.none,
            local=self.none,
            temp=(),
            services=RepoServices(run=run, out=out, err=err),
        )
        return code, json.loads(out.getvalue()) if code == 0 else err.getvalue(), run

    def test_new_local_branch_uses_base_without_main_only_commits(self):
        git("-C", self.seed, "checkout", "-q", "-b", "re-architecture")
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "base only")
        base = git("-C", self.seed, "rev-parse", "HEAD")
        git("-C", self.seed, "push", "-q", "origin", "re-architecture")
        git("-C", self.seed, "checkout", "-q", "main")
        main_only = self.advance()
        for branch, selected, want in (
            ("TASK-1-x", "re-architecture", base),
            ("TASK-2-x", None, main_only),
        ):
            with self.subTest(base=selected):
                code, result, _ = self.worktree(branch, base_branch=selected)
                self.assertEqual(code, 0, result)
                self.assertEqual(result["commit"], want)
                self.assertIsNone(upstream(result["worktree"]))
                if selected:
                    self.assertNotIn(main_only, git("-C", result["worktree"], "rev-list", "HEAD"))

    def test_explicit_base_preserves_local_and_remote_worktree_branches(self):
        local = self.advance()
        git("-C", self.clone, "fetch", "-q", "origin")
        git("-C", self.clone, "branch", "--no-track", "TASK-1-remote", "origin/main")
        for branch, want, tracking in (
            ("TASK-1-remote", local, None),
            (
                "TASK-2-remote",
                git("-C", self.seed, "rev-parse", "TASK-1-remote"),
                "origin/TASK-2-remote",
            ),
        ):
            with self.subTest(branch=branch):
                if tracking:
                    git("-C", self.seed, "push", "-q", "origin", "TASK-1-remote:TASK-2-remote")
                code, result, _ = self.worktree(branch, base_branch="missing")
                self.assertEqual(code, 0, result)
                self.assertEqual(result["commit"], want)
                self.assertEqual(upstream(result["worktree"]), tracking)
                code, reused, run = self.worktree(branch, base_branch="main")
                self.assertEqual(code, 0, reused)
                self.assertEqual(reused["commit"], want)
                self.assertFalse(any("add" in c or "checkout" in c for c in run.calls))

    def test_missing_remote_base_leaves_no_local_branch(self):
        code, err, _ = self.worktree(base_branch="missing")
        self.assertEqual(code, 1, err)
        self.assertIn("origin/missing", err)
        self.assertEqual(git("-C", self.clone, "branch", "--list", "TASK-1-x"), "")
        self.assertFalse(os.path.exists(self.wt))

    def test_a_path_or_a_tilde_path(self):
        head = git("-C", self.clone, "rev-parse", "HEAD")
        with unittest.mock.patch.dict(os.environ, {"HOME": self.tmp}):
            for i, spec in enumerate((self.clone, "~/clone")):
                with self.subTest(spec=spec):
                    base = os.path.join(self.tmp, f"w{i}")
                    wt = os.path.join(base, "o", f"n-TASK-1-{i}")
                    code, r, _ = self.worktree(f"TASK-1-{i}", spec=spec, base=base)
                    self.assertEqual(code, 0, r)
                    self.assertEqual(
                        r,
                        {
                            "repo": "o/n",
                            "host": "github.com",
                            "default": "main",
                            "branch": f"TASK-1-{i}",
                            "commit": head,
                            "worktree": wt,
                            "permalink_base": f"https://github.com/o/n/blob/{head}/",
                            "push": True,
                        },
                    )
                    self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))
                    self.assertEqual(
                        git("-C", wt, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                        os.path.join(self.clone, ".git"),
                    )

    def test_not_a_clone_is_invalid(self):
        linked, plain, bare = (os.path.join(self.tmp, d) for d in ("linked", "plain", "no-origin"))
        git("-C", self.clone, "worktree", "add", "-q", "-b", "TASK-9-y", linked)
        os.makedirs(plain)
        git("init", "-q", bare)
        for spec in (os.path.join(self.tmp, "missing"), plain, linked, bare):
            with self.subTest(spec=spec):
                code, err, run = self.worktree(spec=spec)
                self.assertEqual(code, 2, err)
                self.assertFalse(run.ran("gh"))
                self.assertFalse(os.path.exists(self.dir))

    def test_fetch_leaves_the_clone_alone(self):
        with open(os.path.join(self.clone, "dirty.txt"), "w") as f:
            f.write("x")
        git("-C", self.clone, "commit", "-q", "--allow-empty", "-m", "local")

        def state():
            return [
                git("-C", self.clone, *a)
                for a in (
                    ["rev-parse", "main"],
                    ["branch", "--show-current"],
                    ["status", "--porcelain"],
                )
            ]

        before = state()
        new = self.advance()
        code, r, _ = self.worktree()
        self.assertEqual((code, r["commit"]), (0, new))
        self.assertEqual(git("-C", self.clone, "rev-parse", "origin/main"), new)
        self.assertEqual(state(), before)

    def test_branch_rules_via_worktree_add(self):
        old = git("-C", self.clone, "rev-parse", "HEAD")
        git("-C", self.clone, "branch", "TASK-1-local")
        new = self.advance()
        for branch, commit, tracks in (
            ("TASK-1-local", old, None),
            ("TASK-1-remote", old, "origin/TASK-1-remote"),
            ("TASK-1-new", new, None),
        ):
            with self.subTest(branch=branch):
                base = os.path.join(self.tmp, branch)
                wt = os.path.join(base, "o", f"n-{branch}")
                code, r, run = self.worktree(branch, base=base)
                self.assertEqual((code, r["commit"], r["worktree"]), (0, commit, wt))
                self.assertEqual(run.timeout("git", "-C", self.clone, "worktree", "add"), repo.LONG)
                self.assertEqual(
                    (git("-C", wt, "branch", "--show-current"), upstream(wt)), (branch, tracks)
                )

    def test_reuse_fetches_only(self):
        self.assertEqual(self.worktree()[0], 0)
        new = self.advance()
        code, r, run = self.worktree()
        self.assertEqual((code, r["worktree"]), (0, self.wt))
        self.assertTrue(run.ran("git", "-C", self.wt, *FETCH))
        self.assertFalse(any("add" in c or "checkout" in c for c in run.calls))
        self.assertEqual(git("-C", self.clone, "rev-parse", "origin/main"), new)

    def test_a_worktree_of_another_clone_is_invalid(self):
        other = os.path.join(self.tmp, "clone2")
        git("clone", "-q", self.bare, other)
        git("-C", other, "worktree", "add", "-q", "-b", "TASK-1-x", self.wt)
        code, err, _ = self.worktree()
        self.assertEqual(code, 2)
        self.assertIn("not a checkout of github.com/o/n", err)

    def test_a_full_clone_at_the_place_is_used(self):
        git("clone", "-q", self.bare, self.wt)
        code, r, run = self.worktree()
        self.assertEqual((code, r["worktree"]), (0, self.wt))
        self.assertTrue(os.path.isdir(os.path.join(self.wt, ".git")))
        self.assertTrue(
            run.ran("git", "-C", self.wt, "checkout", "--no-track", "-b", "TASK-1-x", "origin/main")
        )
        self.assertFalse(run.ran("git", "-C", self.clone, "worktree", "add"))

    def test_a_branch_checked_out_elsewhere_is_invalid(self):
        other = os.path.join(self.tmp, "elsewhere")
        git("-C", self.clone, "worktree", "add", "-q", "-b", "TASK-1-x", other)
        code, err, _ = self.worktree()
        self.assertEqual(code, 2)
        self.assertIn(f"TASK-1-x is checked out in {other}", err)
        self.assertFalse(os.path.exists(self.wt))

    def test_two_runs_at_once_on_one_clone(self):
        self.advance()
        results = {}

        def go(branch):
            try:
                results[branch] = repo.worktree(
                    CheckoutRequest(self.clone, branch, os.path.join(self.tmp, branch)),
                    run=Real([info()]),
                    temp=(),
                )
            except Exception as e:
                results[branch] = e

        threads = [threading.Thread(target=go, args=(b,)) for b in ("TASK-1-a", "TASK-2-b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for b, r in results.items():
            self.assertIsInstance(r, dict, r)
            self.assertEqual(git("-C", r["worktree"], "branch", "--show-current"), b)
        self.assertEqual(len(results), 2)

    def test_name_names_the_worktree_for_worktree_and_status(self):
        wt = os.path.join(self.dir, "o", "n-TASK-7")
        run, out, err = Real([info()]), io.StringIO(), io.StringIO()
        code = repo.main(
            ["worktree", "--dir", self.dir, "--branch", "TASK-7-x", "--name", "TASK-7", self.clone],
            config=self.none,
            local=self.none,
            temp=(),
            services=RepoServices(run=run, out=out, err=err),
        )
        self.assertEqual((code, json.loads(out.getvalue())["worktree"]), (0, wt), err.getvalue())
        admin = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        self.assertEqual(admin, os.path.join(self.clone, ".git", "worktrees", "n-TASK-7"))
        for argv, want in ((["--name", "TASK-7"], 0), ([], 2)):
            with self.subTest(argv=argv):
                run, out, err = Real([LOGIN, pr_list([]), info()]), io.StringIO(), io.StringIO()
                code = repo.main(
                    ["status", "--dir", self.dir, "--branch", "TASK-7-x", *argv, self.clone],
                    config=self.none,
                    local=self.none,
                    services=RepoServices(run=run, out=out, err=err),
                )
                self.assertEqual(code, want, err.getvalue())
                if want == 0:  # status with a local path
                    self.assertEqual(
                        {k: json.loads(out.getvalue())[k] for k in ("pr", "base")},
                        {"pr": None, "base": "main"},
                    )
                    self.assertTrue(run.ran("gh", "pr", "list", "--repo", "github.com/o/n"))
