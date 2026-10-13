"""delegate/src/agent_pm/delegate/team/tools/repo.py, loaded by file path as agent runs run it,
ported from the old core's tests (its tmux display helpers dropped; its config, path, shell, git and
repo spec helpers' cases: unit/utils/).
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import unittest.mock

from agent_pm.delegate.composer import TEAM
from agent_pm.delegate.team.tools.types import CheckoutRequest, RepoServices

from delegate.tests.case import Case

LOCAL = "config.local.toml"
SHA = "a" * 40
URL = "https://github.com/o/n.git"
SPEC = importlib.util.spec_from_file_location("repo", os.path.join(TEAM, "tools", "repo.py"))
repo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repo)
FETCH = ("fetch", "--refmap=", "origin", "+refs/heads/*:refs/remotes/origin/*")


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


def fail(stderr):
    return subprocess.CompletedProcess([], 1, "", stderr)


class Fake:
    """A `run` that answers by argv prefix (a list answers in turn) and records every call; `gh repo
    clone` makes the checkout's .git.
    """

    def __init__(self, answers):
        self.answers, self.calls, self.timeouts = answers, [], []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        self.timeouts.append(timeout)
        if argv[:3] == ["gh", "repo", "clone"]:
            os.makedirs(os.path.join(argv[4], ".git"))
        for prefix, res in self.answers:
            if prefix(argv) if callable(prefix) else argv[: len(prefix)] == prefix:
                return res.pop(0) if isinstance(res, list) else res
        return ok()

    def ran(self, *prefix):
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)

    def timeout(self, *prefix):
        return next(
            t
            for c, t in zip(self.calls, self.timeouts, strict=False)
            if c[: len(prefix)] == list(prefix)
        )


class Real(Fake):
    """Real git; `gh` as in Fake; an origin's URL reads as URL."""

    def __call__(self, argv, timeout):
        if argv[0] != "git":
            return super().__call__(argv, timeout)
        self.calls.append(argv)
        self.timeouts.append(timeout)
        for prefix, res in self.answers:
            if callable(prefix) and prefix(argv):
                return res
        res = repo.sh(argv, timeout)
        if argv[-3:] == ["remote", "get-url", "origin"] and res.returncode == 0:
            return ok(URL + "\n")
        return res


def info(push=True, default="main"):
    return (
        ["gh", "api", "--hostname"],
        ok(json.dumps({"permissions": {"push": push}, "default_branch": default})),
    )


HEAD = (lambda a: a[3:] == ["rev-parse", "HEAD"], ok(SHA + "\n"))
LOGIN = (["gh", "api", "--hostname", "github.com", "user"], ok('{"login": "me"}'))
NO_FEEDBACK = (lambda a: "--paginate" in a, ok("[[]]"))
REPO_API = ("gh", "api", "--hostname", "github.com", "repos/o/n")


def pr_list(rows):
    return (["gh", "pr", "list"], ok(json.dumps(rows)))


def pr(base="main"):
    """The branch's own PR 7."""
    return {
        "number": 7,
        "url": "u7",
        "state": "OPEN",
        "baseRefName": base,
        "isCrossRepository": False,
        "author": {"login": "me"},
    }


def git(*argv):
    return subprocess.run(["git", *argv], check=True, capture_output=True, text=True).stdout.strip()


def upstream(wt):
    res = subprocess.run(
        ["git", "-C", wt, "rev-parse", "--abbrev-ref", "@{upstream}"],
        capture_output=True,
        text=True,
    )
    return res.stdout.strip() if res.returncode == 0 else None


class Base(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.wt = os.path.join(self.dir, "o", "n-TASK-1-x")
        self.none = os.devnull

    def existing(self, url=URL):
        os.makedirs(os.path.join(self.wt, ".git"), exist_ok=True)
        return (lambda a: a[3:] == ["remote", "get-url", "origin"], ok(url + "\n"))

    def main(self, argv, run, config=None, local=None):
        out, err = io.StringIO(), io.StringIO()
        code = repo.main(
            argv,
            config=config or self.none,
            local=local or self.none,
            services=RepoServices(run=run, out=out, err=err),
        )
        return code, out.getvalue(), err.getvalue()

    def config(self, text):
        path = os.path.join(self.dir, "config.toml")
        with open(path, "w") as f:
            f.write(text)
        return path


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


class Clone(Case):
    """A real clone of a bare repo, its origin reading as github.com/o/n."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        env = unittest.mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.seed, self.bare, self.clone = (
            os.path.join(self.tmp, d) for d in ("seed", "origin.git", "clone")
        )
        git("init", "-q", "-b", "main", self.seed)
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "one")
        git("-C", self.seed, "branch", "TASK-1-remote")
        git("clone", "-q", "--bare", self.seed, self.bare)
        git("-C", self.seed, "remote", "add", "origin", self.bare)
        git("clone", "-q", self.bare, self.clone)
        self.dir = os.path.join(self.tmp, "work", "src")
        self.wt = os.path.join(self.dir, "o", "n-TASK-1-x")
        self.none = os.devnull

    def listing(self, *paths):
        """A config.toml whose trusted_dirs lists `paths`."""
        path = os.path.join(self.tmp, "config.toml")
        with open(path, "w") as f:
            f.write(f"trusted_dirs = {json.dumps(paths)}\n")
        return path

    def advance(self):
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "two")
        git("-C", self.seed, "push", "-q", "origin", "main")
        return git("-C", self.seed, "rev-parse", "HEAD")


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


class LocalSafety(Clone):
    def worktree(self, fail_on=None, temp=(), config=None, spec=None, local=None):
        run, out, err = (
            Real([info(), *([(fail_on, fail("boom"))] if fail_on else [])]),
            io.StringIO(),
            io.StringIO(),
        )
        code = repo.main(
            ["worktree", "--dir", self.dir, "--branch", "TASK-1-x", spec or self.clone],
            config=config or self.none,
            local=local or self.none,
            temp=temp,
            services=RepoServices(run=run, out=out, err=err),
        )
        return code, err.getvalue()

    def file_commit(self):
        with open(os.path.join(self.seed, "f"), "w") as fh:
            fh.write("one\n")
        git("-C", self.seed, "add", "f")
        git("-C", self.seed, "commit", "-q", "-m", "f")
        git("-C", self.seed, "push", "-q", "origin", "main")
        git("-C", self.clone, "fetch", "-q")

    def link_commit(self):
        os.symlink("/etc/passwd", os.path.join(self.seed, "link"))
        git("-C", self.seed, "add", "link")
        git("-C", self.seed, "commit", "-q", "-m", "link")
        git("-C", self.seed, "push", "-q", "origin", "main")
        return os.path.join(self.wt, "link")

    def assertGood(self):
        index = git("-C", self.wt, "rev-parse", "--path-format=absolute", "--git-path", "index")
        self.assertTrue(os.path.isfile(index))
        self.assertTrue(os.path.isfile(os.path.join(self.wt, "f")))
        self.assertEqual(git("-C", self.wt, "status", "--porcelain"), "")
        self.assertEqual(
            git("-C", self.wt, "config", "--worktree", "--get", "core.symlinks"), "false"
        )

    def symlinks(self, wt=None):
        """Worktree `wt`'s own core.symlinks, else the clone's; None when unset."""
        res = subprocess.run(
            [
                "git",
                "-C",
                wt or self.clone,
                "config",
                *(["--worktree"] if wt else []),
                "--get",
                "core.symlinks",
            ],
            capture_output=True,
            text=True,
        )
        return res.stdout.strip() if res.returncode == 0 else None

    def remove(self):
        shutil.rmtree(self.wt, ignore_errors=True)
        git("-C", self.clone, "worktree", "prune")

    def half_made(self):
        """A worktree whose checkout never finished."""
        git("-C", self.clone, "config", "extensions.worktreeConfig", "true")
        git(
            "-C",
            self.clone,
            "worktree",
            "add",
            "-q",
            "--no-checkout",
            "-b",
            "TASK-1-x",
            self.wt,
            "origin/main",
        )

    def test_a_local_clone_under_a_temp_dir_is_refused(self):
        code, err = self.worktree(temp=(self.tmp,))
        self.assertEqual(code, 2)
        self.assertIn("temp dir", err)
        self.assertFalse(os.path.exists(self.wt))

    def test_a_failed_step_after_the_add_leaves_no_worktree_and_a_rerun_succeeds(self):
        self.file_commit()
        for step, fail_on in (
            ("config", lambda a: a[3:5] == ["config", "--worktree"] and "--get" not in a),
            ("reset", lambda a: a[3:4] == ["reset"]),
        ):
            with self.subTest(step):
                code, err = self.worktree(fail_on=fail_on)
                self.assertEqual(code, 1, err)
                self.assertFalse(os.path.exists(self.wt))
                self.assertNotIn(self.wt, git("-C", self.clone, "worktree", "list", "--porcelain"))
                self.assertEqual(self.worktree(), (0, ""))
                self.assertGood()
                self.remove()

    def test_a_half_made_worktree_is_made_again(self):
        self.file_commit()
        self.half_made()
        self.assertEqual(self.worktree(), (0, ""))
        self.assertGood()

    def test_a_half_made_worktree_of_a_listed_clone_is_made_again_with_symlinks(self):
        link = self.link_commit()
        git("-C", self.clone, "fetch", "-q")
        self.half_made()
        self.assertEqual(self.worktree(config=self.listing(self.clone)), (0, ""))
        self.assertEqual(
            git("-C", self.wt, "config", "--worktree", "--get", "core.symlinks"), "true"
        )
        self.assertEqual(os.readlink(link), "/etc/passwd")

    def test_only_a_listed_clones_worktree_gets_symlinks_and_the_clone_keeps_its_config(self):
        """trusted_dirs lists clones by real path, in the config or its local file; listing another
        clone of the same origin trusts no other.
        """
        link = self.link_commit()
        other, alias = os.path.join(self.tmp, "clone2"), os.path.join(self.tmp, "alias")
        git("clone", "-q", self.bare, other)
        os.symlink(self.clone, alias)
        local = os.path.join(self.tmp, "config.local.toml")
        for case, listed, in_local, spec, on in (
            ("none listed", None, False, None, False),
            ("listed", [self.clone], False, None, True),
            ("another clone of the origin listed", [other], False, None, False),
            ("listed by an alias", [alias], False, None, True),
            ("listed, given by an alias", [self.clone], False, alias, True),
            ("listed in the local file", [], True, None, True),
        ):
            with self.subTest(case):
                if in_local:
                    with open(local, "w") as f:
                        f.write(f"trusted_dirs = {json.dumps([self.clone])}\n")
                try:
                    config = None if listed is None else self.listing(*listed)
                    self.assertEqual(self.worktree(config=config, spec=spec, local=local), (0, ""))
                    if on:
                        self.assertEqual(os.readlink(link), "/etc/passwd")
                    else:
                        self.assertTrue(os.path.isfile(link) and not os.path.islink(link))
                    for scope in (["--worktree"], []):  # set per worktree, and so in effect
                        self.assertEqual(
                            git("-C", self.wt, "config", *scope, "--get", "core.symlinks"),
                            str(on).lower(),
                        )
                    self.assertEqual(git("-C", self.wt, "status", "--porcelain"), "")
                    self.assertIsNone(self.symlinks())
                    self.assertEqual(git("-C", self.clone, "status", "--porcelain"), "")
                finally:
                    self.remove()
                    if in_local:
                        os.remove(local)

    def test_an_existing_worktree_must_have_the_listed_setting(self):
        """Made by git (unset), unlisted (false) or listed (true): run with the other setting,
        refused and unchanged.
        """
        self.file_commit()
        listed = self.listing(self.clone)
        for made, run_config, found, gives in (
            ("by git", None, "unset", "false"),
            ("unlisted", listed, "false", "true"),
            ("listed", None, "true", "false"),
        ):
            with self.subTest(made=made):
                if made == "by git":
                    git(
                        "-C",
                        self.clone,
                        "worktree",
                        "add",
                        "-q",
                        "-b",
                        "TASK-1-x",
                        self.wt,
                        "origin/main",
                    )
                else:
                    self.assertEqual(
                        self.worktree(config=listed if made == "listed" else None), (0, "")
                    )
                code, err = self.worktree(config=run_config)
                self.assertEqual(code, 2, err)
                for text in (
                    f"core.symlinks={found}",
                    f"trusted_dirs gives {gives}",
                    "remove it and run again",
                ):
                    self.assertIn(text, err)
                self.assertEqual(self.symlinks(self.wt), None if found == "unset" else found)
                self.remove()
        self.assertEqual(self.worktree(config=listed), (0, ""))
        self.assertEqual(self.worktree(config=listed), (0, ""))

    def test_a_worktree_deleted_by_hand_is_added_again(self):
        other = os.path.join(self.tmp, "users-own")
        git("-C", self.clone, "worktree", "add", "-q", "-b", "mine", other)
        shutil.rmtree(other)
        self.assertEqual(self.worktree()[0], 0)
        shutil.rmtree(self.wt)
        self.assertEqual(self.worktree(), (0, ""))
        self.assertEqual(git("-C", self.wt, "branch", "--show-current"), "TASK-1-x")
        self.assertIn(f"worktree {other}", git("-C", self.clone, "worktree", "list", "--porcelain"))

    def test_a_mirror_refspec_cannot_move_the_clones_branches(self):
        git("-C", self.clone, "branch", "TASK-9-mine")
        mine = git("-C", self.clone, "rev-parse", "TASK-9-mine")
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "theirs")
        git("-C", self.seed, "push", "-q", "origin", "HEAD:TASK-9-mine")
        git("-C", self.clone, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
        self.assertEqual(self.worktree()[0], 0)
        self.assertEqual(git("-C", self.clone, "rev-parse", "TASK-9-mine"), mine)


class PlanDocs(Case):
    def test_nested_checkouts_node_modules_and_git_are_skipped_but_not_dot_dirs(self):
        """A subdir holding a .git file (a worktree) or dir (a clone) is a checkout of its own."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wt = tmp.name

        def doc(*parts):
            path = os.path.join(wt, *parts, "plan.md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("RESUME: phase=S5 branch=b\n")
            return path

        found = doc("docs", ".autopilot")
        for parts in (
            ("wt", "x", "worktrees", "y"),
            ("vendor", "clone"),
            ("node_modules", "p"),
            (".git",),
        ):
            doc(*parts)
        with open(os.path.join(wt, "wt", "x", "worktrees", "y", ".git"), "w") as f:
            f.write("gitdir: /elsewhere/.git/worktrees/y\n")
        os.makedirs(os.path.join(wt, "vendor", "clone", ".git"))
        self.assertEqual(repo.plan_docs(wt, "b"), [{"path": found, "phase": "S5"}])


class Status(Base):
    def plan_doc(self, branch="TASK-1-x", phase="S8"):
        os.makedirs(os.path.join(self.wt, "docs"), exist_ok=True)
        path = os.path.join(self.wt, "docs", "plan.md")
        with open(path, "w") as f:
            f.write(f"# Plan\n\nRESUME: phase={phase} branch={branch}\n")
        return path

    def status(self, *answers, argv=(), config=None, local=None):
        """(exit code, the output or stderr, run) of status on a checkout of o/n; `answers` after
        the gh login's.
        """
        run = Fake([self.existing(), LOGIN, *answers])
        code, out, err = self.main(
            ["status", "o/n", "--branch", "TASK-1-x", "--dir", self.dir, *argv], run, config, local
        )
        return code, json.loads(out) if code == 0 else err, run

    def test_pr_plan_docs_and_feedback_since_the_latest_plan_doc_commit(self):
        path = self.plan_doc()
        comments = [
            [
                {"created_at": "2026-10-01T00:00:00Z", "user": {"login": "me"}, "body": "old"},
                {"created_at": "2026-10-03T00:00:00Z", "user": {"login": "me"}, "body": "do Y"},
            ]
        ]
        reviews = [
            [
                {
                    "submitted_at": "2026-10-03T01:00:00Z",
                    "user": {"login": "bot"},
                    "body": "nit",
                    "state": "COMMENTED",
                },
                {"submitted_at": None, "user": {"login": "bot"}, "body": "pending"},
            ]
        ]
        code, r, run = self.status(
            (lambda a: a[3:5] == ["log", "-1"], ok("2026-10-02T00:00:00+00:00\n")),
            pr_list(
                [
                    {
                        "number": 9,
                        "url": "u9",
                        "state": "OPEN",
                        "isCrossRepository": True,
                        "author": {"login": "me"},
                    },
                    pr("TASK-0-a"),
                ]
            ),
            (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
            (lambda a: a[-1].endswith("/pulls/7/reviews"), ok(json.dumps(reviews))),
            (lambda a: a[-1].endswith("/pulls/7/comments"), ok("[[]]")),
        )
        self.assertEqual(code, 0, r)
        self.assertEqual(
            r["pr"], {"number": 7, "url": "u7", "state": "OPEN", "baseRefName": "TASK-0-a"}
        )
        self.assertIn(
            "number,url,state,baseRefName,isCrossRepository,author",
            next(c for c in run.calls if c[1] == "pr"),
        )
        self.assertEqual(r["plan_docs"], [{"path": path, "phase": "S8"}])
        self.assertEqual([e["body"] for e in r["user"]], ["do Y"])
        self.assertEqual([(e["body"], e["state"]) for e in r["others"]], [("nit", "COMMENTED")])

    def test_configured_users_replace_the_gh_login(self):
        """From the config and local passed only, never the HOME one (malformed here)."""
        comments = [
            [
                {
                    "created_at": "2026-10-03T00:00:00Z",
                    "user": {"login": "me"},
                    "body": "from the bot",
                },
                {"created_at": "2026-10-03T01:00:00Z", "user": {"login": "Alice"}, "body": "do Y"},
            ]
        ]
        with open(os.path.join(self.agent_pm, LOCAL), "w") as f:
            f.write("malformed = [\n")
        local = os.path.join(self.dir, "config.local.toml")
        with open(local, "w") as f:
            f.write('users = ["alice"]\n')
        for text, given in (('users = ["alice"]\n', os.devnull), ('users = ["bob"]\n', local)):
            with self.subTest(local=given):
                code, r, _ = self.status(
                    pr_list([pr()]),
                    (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                    NO_FEEDBACK,
                    config=self.config(text),
                    local=given,
                )
                self.assertEqual((code, r["pr"]["number"]), (0, 7), r)
                self.assertEqual([e["body"] for e in r["user"]], ["do Y"])
                self.assertEqual([e["body"] for e in r["others"]], ["from the bot"])
        with self.subTest("users = [] is the gh login"):
            code, r, _ = self.status(
                pr_list([pr()]),
                (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                NO_FEEDBACK,
                config=self.config("users = []\n"),
                local=os.devnull,
            )
            self.assertEqual((code, [e["body"] for e in r["user"]]), (0, ["from the bot"]), r)
            self.assertEqual([e["body"] for e in r["others"]], ["do Y"])

    def test_other_branches_plan_docs_and_no_pr(self):
        self.plan_doc(branch="other")
        self.assertEqual(
            self.status(pr_list([]), info())[:2],
            (
                0,
                {
                    "pr": None,
                    "base": "main",
                    "plan_docs": [],
                    "since": None,
                    "user": [],
                    "others": [],
                },
            ),
        )

    def test_an_invalid_trusted_dirs_does_not_affect_status(self):
        code, err, _ = self.status(
            pr_list([]), info(), config=self.config('trusted_dirs = ["a/b"]\n')
        )
        self.assertEqual(code, 0, err)

    def test_bad_config_is_reported_before_an_unreadable_repo(self):
        code, _, _ = self.main(
            ["status", "--dir", self.dir, "--branch", "b", "not a repo"],
            Fake([]),
            self.config("users = [\n"),
        )
        self.assertEqual(code, 1)

    def test_the_defaults_load_the_local_file(self):
        with open(os.path.join(self.agent_pm, "config.local.toml"), "w") as stream:
            stream.write("users = [\n")
        errors = io.StringIO()
        code = repo.main(
            ["status", "o/n", "--branch", "b", "--dir", self.dir],
            services=RepoServices(run=Fake([]), out=io.StringIO(), err=errors),
        )
        self.assertEqual(code, 1)
        self.assertIn("Invalid value", errors.getvalue())

    def test_needs_a_checkout(self):
        code, _, err = self.main(["status", "o/n", "--branch", "b", "--dir", self.dir], Fake([]))
        self.assertEqual(code, 2)
        self.assertIn("run worktree first", err)

    def test_base_is_the_prs_else_the_option_else_the_default(self):
        for prs, argv, want, asks in (
            ([pr("TASK-0-a")], ["--base", "TASK-0-b"], "TASK-0-a", False),
            ([], ["--base", "TASK-0-b"], "TASK-0-b", False),
            ([], [], "trunk", True),
        ):
            with self.subTest(prs=prs, argv=argv):
                code, r, run = self.status(
                    pr_list(prs), NO_FEEDBACK, info(default="trunk"), argv=argv
                )
                self.assertEqual((code, r["base"]), (0, want), r)
                self.assertEqual(run.ran(*REPO_API), asks)

    def test_an_unsafe_base_exits_2(self):
        for prs, argv, default in (
            ([pr("a..b")], [], "main"),
            ([pr(None)], [], "main"),
            ([], ["--base", "x;rm -rf ~"], "main"),
            ([], ["--base", ""], "main"),
            ([], [], "-x"),
        ):
            with self.subTest(prs=prs, argv=argv, default=default):
                code, err, _ = self.status(
                    pr_list(prs), NO_FEEDBACK, info(default=default), argv=argv
                )
                self.assertEqual(code, 2, err)
                self.assertIn("unsafe base branch name", err)


if __name__ == "__main__":
    unittest.main()
