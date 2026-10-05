import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import repo  # noqa: E402

SHA = "a" * 40
URL = "https://github.com/o/n.git"


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


def fail(stderr):
    return subprocess.CompletedProcess([], 1, "", stderr)


class Fake:
    """A `run` that answers by argv prefix (a list answer gives its items in turn) and records every call and its
    timeout; `gh repo clone` makes the checkout's .git."""

    def __init__(self, answers):
        self.answers, self.calls, self.timeouts = answers, [], []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        self.timeouts.append(timeout)
        if argv[:3] == ["gh", "repo", "clone"]:
            os.makedirs(os.path.join(argv[4], ".git"))
        for prefix, res in self.answers:
            if prefix(argv) if callable(prefix) else argv[:len(prefix)] == prefix:
                return res.pop(0) if isinstance(res, list) else res
        return ok()

    def ran(self, *prefix):
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)

    def timeout(self, *prefix):
        return next(t for c, t in zip(self.calls, self.timeouts) if c[:len(prefix)] == list(prefix))


class Real(Fake):
    """Real git, `gh` answered as Fake does; origin's URL, when there is one, reads as URL."""

    def __call__(self, argv, timeout):
        if argv[0] != "git":
            return super().__call__(argv, timeout)
        self.calls.append(argv)
        self.timeouts.append(timeout)
        res = repo.sh(argv, timeout)
        if argv[-3:] == ["remote", "get-url", "origin"] and res.returncode == 0:
            return ok(URL + "\n")
        return res


def info(push=True, default="main"):
    return (["gh", "api", "--hostname"], ok(json.dumps({"permissions": {"push": push}, "default_branch": default})))


HEAD = (lambda a: a[3:] == ["rev-parse", "HEAD"], ok(SHA + "\n"))


def git(*argv):
    return subprocess.run(["git", *argv], check=True, capture_output=True, text=True).stdout.strip()


def upstream(wt):
    res = subprocess.run(["git", "-C", wt, "rev-parse", "--abbrev-ref", "@{upstream}"], capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else None


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.wt = os.path.join(self.dir, "o", "n")

    def existing(self, url=URL):
        os.makedirs(os.path.join(self.wt, ".git"))
        return (lambda a: a[3:] == ["remote", "get-url", "origin"], ok(url + "\n"))

    def main(self, argv, run):
        out, err = io.StringIO(), io.StringIO()
        code = repo.main(argv, run=run, out=out, err=err)
        return code, out.getvalue(), err.getvalue()


class Parse(unittest.TestCase):
    def test_forms(self):
        for spec, want in (("o/n", ("github.com", "o", "n")), ("ghe.example.com/o/n", ("ghe.example.com", "o", "n")),
                           ("https://github.com/o/n.git", ("github.com", "o", "n")), ("https://GHE.io/o/n/", ("ghe.io", "o", "n"))):
            self.assertEqual(repo.Repo.parse(spec), repo.Repo(*want), spec)

    def test_unreadable(self):
        for spec in ("n", "o/..", "https://o/n", "o/n; rm -rf /", "-o/n"):
            with self.assertRaises(repo.Invalid, msg=spec):
                repo.Repo.parse(spec)

    def test_origin(self):
        self.assertEqual(repo.origin("git@GitHub.com:O/N.git"), "github.com/o/n")
        self.assertEqual(repo.origin("https://ghe.io/o/n"), "ghe.io/o/n")
        self.assertEqual(repo.url_slug("git@GitHub.com:O/N.git"), "GitHub.com/O/N")
        self.assertIsNone(repo.url_slug("/tmp/origin.git"))


class ErrText(unittest.TestCase):
    def test_trimmed_and_capped(self):
        for stderr, want in ((None, ""), ("", ""), ("  HTTP 404: Not Found \n", "HTTP 404: Not Found"), ("x" * 300, "x" * 200)):
            with self.subTest(stderr=stderr):
                self.assertEqual(repo.err_text(subprocess.CompletedProcess([], 1, stderr=stderr)), want)


class Worktree(Base):
    def worktree(self, *answers, branch="TASK-1-x", spec="o/n"):
        run = Fake([*answers, HEAD])
        return self.main(["worktree", spec, f"--branch={branch}", "--dir", self.dir], run), run

    def test_fresh_blobless_clone_on_a_new_branch_from_the_default(self):
        (code, out, _), run = self.worktree(info(default="trunk"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"repo": "o/n", "host": "github.com", "default": "trunk", "branch": "TASK-1-x",
                                           "commit": SHA, "worktree": self.wt,
                                           "permalink_base": f"https://github.com/o/n/blob/{SHA}/", "push": True})
        self.assertIn(["gh", "repo", "clone", "github.com/o/n", self.wt, "--", "-c", "core.symlinks=false", "--filter=blob:none"],
                      run.calls)
        self.assertTrue(run.ran("git", "-C", self.wt, "checkout", "--no-track", "-b", "TASK-1-x", "origin/trunk"))
        self.assertEqual(run.timeout("git", "-C", self.wt, "checkout"), repo.LONG)

    def test_remote_branch_is_tracked(self):
        (code, _, _), run = self.worktree(info(), (lambda a: "ls-remote" in a, ok("abc\trefs/heads/TASK-1-x\n")))
        self.assertEqual(code, 0)
        self.assertTrue(run.ran("git", "-C", self.wt, "ls-remote", "--heads", "origin", "refs/heads/TASK-1-x"))
        self.assertTrue(run.ran("git", "-C", self.wt, "checkout", "--track", "-b", "TASK-1-x", "origin/TASK-1-x"))
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
        (code, _, _), run = self.worktree(self.existing(), info(), (lambda a: a[3:5] == ["branch", "--list"], ok("  TASK-1-x\n")))
        self.assertEqual(code, 0)
        self.assertTrue(run.ran("git", "-C", self.wt, "fetch", "origin"))
        self.assertTrue(run.ran("git", "-C", self.wt, "checkout", "TASK-1-x"))
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_already_on_the_branch_changes_nothing(self):
        (code, _, _), run = self.worktree(self.existing(), info(), (lambda a: "--show-current" in a, ok("TASK-1-x\n")))
        self.assertEqual(code, 0)
        self.assertFalse(any("checkout" in c or "worktree" in c for c in run.calls))

    def test_same_name_under_two_owners(self):
        for owner in ("a", "b"):
            code, out, _ = self.main(["worktree", f"{owner}/n", "--branch", "TASK-1-x", "--dir", self.dir], Fake([info(), HEAD]))
            self.assertEqual((code, json.loads(out)["worktree"]), (0, os.path.join(self.dir, owner, "n")))

    def test_other_repo_in_the_way_is_invalid(self):
        (code, _, err), _ = self.worktree(self.existing("https://github.com/x/n"), info())
        self.assertEqual(code, 2)
        self.assertIn("not a checkout of github.com/o/n", err)

    def test_a_worktree_in_the_way_is_invalid_for_a_remote_repo(self):
        os.makedirs(self.wt)
        with open(os.path.join(self.wt, ".git"), "w") as f:
            f.write("gitdir: /elsewhere/.git/worktrees/n\n")
        (code, _, err), run = self.worktree(info())
        self.assertEqual(code, 2)
        self.assertIn("not a checkout of github.com/o/n", err)
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_a_symlink_in_the_way_is_invalid(self):
        os.makedirs(os.path.dirname(self.wt))
        os.symlink(self.dir, self.wt)
        (code, _, err), _ = self.worktree(info())
        self.assertEqual(code, 2)
        self.assertIn("exists and is not a checkout", err)

    def test_not_found_is_invalid(self):
        (code, _, err), run = self.worktree((["gh", "api"], fail("gh: Not Found (HTTP 404)")))
        self.assertEqual(code, 2)
        self.assertIn("HTTP 404", err)
        self.assertFalse(run.ran("gh", "repo", "clone"))

    def test_network_failure_exits_1(self):
        (code, _, err), _ = self.worktree(info(), (["gh", "repo", "clone"], fail("connection reset")))
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
        (code, _, err), run = self.worktree((lambda a: a[3:] == ["remote", "get-url", "origin"], ok("/srv/n.git\n")), spec=clone)
        self.assertEqual(code, 2)
        self.assertIn("no origin URL naming host/owner/name", err)
        self.assertFalse(run.ran("gh"))

    def test_unsafe_branch_is_reported_before_an_unreadable_repo(self):
        code, _, err = self.main(["worktree", "--dir", self.dir, "--branch=a..b", "not a repo"], Fake([]))
        self.assertEqual(code, 2)
        self.assertIn("unsafe branch name", err)

    def test_unsafe_branch_is_invalid(self):
        for branch in ("-x", "a..b", "a b", "x/"):
            (code, _, _), run = self.worktree(info(), branch=branch)
            self.assertEqual(code, 2, branch)
            self.assertEqual(run.calls, [])

    def test_an_option_given_twice_is_refused(self):
        for cmd in ("worktree", "status"):
            for argv, opt in ((["--dir", self.dir, "--branch", "b", "o/n", "--dir", os.path.expanduser("~/.claude")], "--dir"),
                              (["--dir", self.dir, "--branch", "b", "o/n", "--branch", "c"], "--branch")):
                err = io.StringIO()
                with self.subTest(cmd=cmd, opt=opt), self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr", err):
                    repo.main([cmd, *argv], run=Fake([]))
                self.assertIn(f"{opt} given twice", err.getvalue())

    def test_branch_is_required(self):
        for cmd in ("worktree", "status"):
            with self.subTest(cmd=cmd), self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr", io.StringIO()):
                repo.main([cmd, "--dir", self.dir, "o/n"], run=Fake([]))

    def test_prepare_and_checkout_are_gone(self):
        for cmd in ("prepare", "checkout"):
            with self.subTest(cmd=cmd), self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr", io.StringIO()):
                repo.main([cmd, "--dir", self.dir, "--branch", "b", "o/n"], run=Fake([]))


class Retry(unittest.TestCase):
    LOCKS = ("fatal: Unable to create '/w/.git/index.lock': File exists.",
             "error: cannot lock ref 'refs/remotes/origin/main': is at abc but expected def",
             "error: could not lock config file .git/config: File exists")

    def git(self, *results):
        run = Fake([(["git"], list(results))])
        with unittest.mock.patch.object(repo.time, "sleep") as sleep:
            try:
                return repo.git(run, "/w", "fetch", "origin"), run, sleep
            except RuntimeError as e:
                return e, run, sleep

    def test_a_lock_failure_is_retried(self):
        for stderr in self.LOCKS:
            with self.subTest(stderr=stderr):
                out, run, sleep = self.git(fail(stderr), ok("done"))
                self.assertEqual((out, len(run.calls)), ("done", 2))
                sleep.assert_called_once_with(0.5)

    def test_five_lock_failures_raise(self):
        err, run, sleep = self.git(*[fail(self.LOCKS[0])] * 5)
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("index.lock", str(err))
        self.assertEqual(len(run.calls), 5)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.5, 1, 2, 4])

    def test_other_failures_are_not_retried(self):
        err, run, sleep = self.git(fail("fatal: not a git repository"), ok())
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(len(run.calls), 1)
        sleep.assert_not_called()


class Clone(unittest.TestCase):
    """A real clone of a bare repo; `gh` faked, origin's URL read as github.com/o/n."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        env = unittest.mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                                                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        env.start()
        self.addCleanup(env.stop)
        self.seed, self.bare, self.clone = (os.path.join(self.tmp, d) for d in ("seed", "origin.git", "clone"))
        git("init", "-q", "-b", "main", self.seed)
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "one")
        git("-C", self.seed, "branch", "TASK-1-remote")
        git("clone", "-q", "--bare", self.seed, self.bare)
        git("-C", self.seed, "remote", "add", "origin", self.bare)
        git("clone", "-q", self.bare, self.clone)
        self.dir = os.path.join(self.tmp, "work", "src")
        self.wt = os.path.join(self.dir, "o", "n")

    def advance(self):
        git("-C", self.seed, "commit", "-q", "--allow-empty", "-m", "two")
        git("-C", self.seed, "push", "-q", "origin", "main")
        return git("-C", self.seed, "rev-parse", "HEAD")


class Local(Clone):
    def worktree(self, branch="TASK-1-x", spec=None, base=None):
        run, out, err = Real([info()]), io.StringIO(), io.StringIO()
        code = repo.main(["worktree", "--dir", base or self.dir, "--branch", branch, spec or self.clone], run=run, out=out, err=err)
        return code, json.loads(out.getvalue()) if code == 0 else err.getvalue(), run

    def test_a_path_or_a_tilde_path(self):
        head = git("-C", self.clone, "rev-parse", "HEAD")
        with unittest.mock.patch.dict(os.environ, {"HOME": self.tmp}):
            for i, spec in enumerate((self.clone, "~/clone")):
                with self.subTest(spec=spec):
                    base = os.path.join(self.tmp, f"w{i}")
                    wt = os.path.join(base, "o", "n")
                    code, r, _ = self.worktree(f"TASK-1-{i}", spec=spec, base=base)
                    self.assertEqual(code, 0, r)
                    self.assertEqual(r, {"repo": "o/n", "host": "github.com", "default": "main", "branch": f"TASK-1-{i}",
                                         "commit": head, "worktree": wt,
                                         "permalink_base": f"https://github.com/o/n/blob/{head}/", "push": True})
                    self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))
                    self.assertEqual(git("-C", wt, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                                     os.path.join(self.clone, ".git"))

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
            return [git("-C", self.clone, *a) for a in (["rev-parse", "main"], ["branch", "--show-current"], ["status", "--porcelain"])]
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
        for branch, commit, tracks in (("TASK-1-local", old, None), ("TASK-1-remote", old, "origin/TASK-1-remote"),
                                       ("TASK-1-new", new, None)):
            with self.subTest(branch=branch):
                base = os.path.join(self.tmp, branch)
                wt = os.path.join(base, "o", "n")
                code, r, run = self.worktree(branch, base=base)
                self.assertEqual((code, r["commit"], r["worktree"]), (0, commit, wt))
                self.assertEqual(run.timeout("git", "-C", self.clone, "worktree", "add"), repo.LONG)
                self.assertEqual((git("-C", wt, "branch", "--show-current"), upstream(wt)), (branch, tracks))

    def test_reuse_fetches_only(self):
        self.assertEqual(self.worktree()[0], 0)
        new = self.advance()
        code, r, run = self.worktree()
        self.assertEqual((code, r["worktree"]), (0, self.wt))
        self.assertTrue(run.ran("git", "-C", self.wt, "fetch", "origin"))
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
        self.assertTrue(run.ran("git", "-C", self.wt, "checkout", "--no-track", "-b", "TASK-1-x", "origin/main"))
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
                results[branch] = repo.worktree(self.clone, branch, os.path.join(self.tmp, branch), run=Real([info()]))
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

    def test_status_with_a_local_path(self):
        self.assertEqual(self.worktree()[0], 0)
        run = Real([(["gh", "api", "--hostname", "github.com", "user"], ok('{"login": "me"}')), (["gh", "pr", "list"], ok("[]"))])
        out, err = io.StringIO(), io.StringIO()
        code = repo.main(["status", "--dir", self.dir, "--branch", "TASK-1-x", self.clone], run=run, out=out, err=err,
                         config=os.path.join(self.tmp, "none.toml"))
        self.assertEqual(code, 0, err.getvalue())
        self.assertIsNone(json.loads(out.getvalue())["pr"])
        self.assertTrue(run.ran("gh", "pr", "list", "--repo", "github.com/o/n"))


GUARD = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C"]


class Remove(Clone):
    def add(self, branch="TASK-1-x", start="origin/main", at=None, opts=()):
        wt = os.path.join(self.tmp, "w", branch or "detached")
        git("-C", at or self.clone, "worktree", "add", "-q", *opts, *(["-b", branch] if branch else ["--detach"]), wt, start)
        return wt

    def commit(self, wt, push=False):
        git("-C", wt, "commit", "-q", "--allow-empty", "-m", "w")
        if push:
            git("-C", wt, "push", "-q", "origin", "HEAD")

    def remove(self, wt, prefix="TASK-1-"):
        run = Real([])
        try:
            r = repo.remove(wt, prefix, run=run)
        except repo.Invalid as e:
            r = e
        for c in run.calls:
            self.assertEqual(c[:6], GUARD, c)
        return r, run

    def branches(self):
        return git("-C", self.clone, "branch", "--format=%(refname:short)").split()

    def state(self, at=None):
        return [git("-C", at or self.clone, *a) for a in (["worktree", "list", "--porcelain"], ["branch", "--list"])]

    def test_a_pushed_branch_is_deleted(self):
        wt = self.add()
        self.commit(wt, push=True)
        self.assertEqual(self.remove(wt)[0], {"worktree": wt, "branch": "TASK-1-x", "kept": None})
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(self.branches(), ["main"])
        self.assertEqual(git("-C", self.clone, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_a_relative_backlink_is_followed(self):
        try:
            wt = self.add(opts=("--relative-paths",))
        except subprocess.CalledProcessError:
            self.skipTest("git without worktree add --relative-paths")
        self.commit(wt, push=True)
        self.assertEqual(self.remove(wt)[0]["kept"], None)

    def test_a_branch_with_no_own_commits_is_in_origin_head(self):
        wt = self.add()
        self.assertEqual(self.remove(wt)[0], {"worktree": wt, "branch": "TASK-1-x", "kept": None})
        self.assertEqual(self.branches(), ["main"])

    def test_an_unpushed_commit_keeps_the_branch(self):
        for i, pushed in enumerate((False, True)):
            with self.subTest(pushed=pushed):
                wt = self.add(f"TASK-1-{i}")
                if pushed:
                    self.commit(wt, push=True)
                self.commit(wt)
                self.assertEqual(self.remove(wt)[0], {"worktree": wt, "branch": f"TASK-1-{i}", "kept": "not pushed"})
                self.assertFalse(os.path.exists(wt))
                self.assertIn(f"TASK-1-{i}", self.branches())

    def test_without_origin_head_only_the_branch_on_origin_counts(self):
        git("-C", self.clone, "remote", "set-head", "origin", "-d")
        merged, pushed = self.add("TASK-1-merged"), self.add("TASK-1-pushed")
        self.commit(pushed, push=True)
        self.assertEqual(self.remove(merged)[0]["kept"], "not pushed")
        self.assertEqual(self.remove(pushed)[0]["kept"], None)

    def test_a_dirty_worktree_is_refused_and_kept(self):
        for change in ("tracked", "untracked"):
            with self.subTest(change=change):
                wt = self.add(f"TASK-1-{change}")
                path = os.path.join(wt, "f.txt")
                if change == "tracked":
                    with open(path, "w") as f:
                        f.write("x")
                    git("-C", wt, "add", "f.txt")
                    git("-C", wt, "commit", "-q", "-m", "f")
                with open(path, "w") as f:
                    f.write("y")
                before = self.state()
                r, _ = self.remove(wt)
                self.assertIsInstance(r, repo.Invalid)
                self.assertTrue(str(r).startswith(f"{wt}: "), r)
                self.assertEqual(self.state(), before)
                self.assertTrue(os.path.isfile(path))

    def test_ignored_files_do_not_block(self):
        wt = self.add()
        os.makedirs(os.path.join(self.clone, ".git", "info"), exist_ok=True)
        with open(os.path.join(self.clone, ".git", "info", "exclude"), "a") as f:
            f.write("*.log\n")
        open(os.path.join(wt, "x.log"), "w").close()
        self.assertEqual(self.remove(wt)[0]["kept"], None)
        self.assertFalse(os.path.exists(wt))

    def test_not_a_worktree_is_invalid(self):
        wt = self.add()
        plain, linked, junk, alias = (os.path.join(self.tmp, d) for d in ("plain", "linked", "junk", "alias"))
        for d in (plain, linked, junk):
            os.makedirs(d)
        os.symlink(os.path.join(wt, ".git"), os.path.join(linked, ".git"))
        with open(os.path.join(junk, ".git"), "w") as f:
            f.write("nonsense\n")
        os.symlink(wt, alias)
        before = self.state()
        for path in (self.clone, plain, linked, junk, alias, os.path.join(self.tmp, "missing")):
            with self.subTest(path=path):
                r, run = self.remove(path)
                self.assertIsInstance(r, repo.Invalid)
                self.assertTrue(all("rev-parse" in c for c in run.calls))
        self.assertEqual(self.state(), before)
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))

    def test_a_forged_git_file_is_invalid_and_nothing_is_touched(self):
        real = self.add()
        admin = git("-C", real, "rev-parse", "--path-format=absolute", "--git-dir")
        agent = os.path.join(self.tmp, "agent.git")
        git("clone", "-q", "--bare", self.bare, agent)
        bare_wt = self.add("TASK-1-y", "main", at=agent)
        os.makedirs(os.path.join(self.tmp, "fake"))
        os.symlink(agent, os.path.join(self.tmp, "fake", ".git"))
        forged = os.path.join(self.tmp, "forged")
        os.makedirs(forged)
        with open(os.path.join(self.clone, ".git", "gitdir"), "w") as f:
            f.write(os.path.join(forged, ".git") + "\n")
        cases = {"gitdir outside worktrees/": (forged, f"{self.clone}/.git"), "wrong backlink": (forged, admin),
                 "symlinked common dir": (bare_wt, f"{self.tmp}/fake/.git/worktrees/TASK-1-y")}
        before = self.state(), self.state(agent)
        for name, (wt, gitdir) in cases.items():
            with self.subTest(name):
                with open(os.path.join(wt, ".git"), "w") as f:
                    f.write(f"gitdir: {gitdir}\n")
                r, run = self.remove(wt)
                self.assertIsInstance(r, repo.Invalid)
                self.assertEqual([c[7] for c in run.calls], ["rev-parse"])
                self.assertTrue(os.path.isdir(wt))
        self.assertEqual((self.state(), self.state(agent)), before)
        self.assertTrue(os.path.isdir(real))

    def test_an_agent_built_repo_is_the_only_one_touched(self):
        agent = os.path.join(self.tmp, "agent")
        git("init", "-q", "-b", "main", agent)
        git("-C", agent, "commit", "-q", "--allow-empty", "-m", "a")
        wt = self.add(start="main", at=agent)
        before = self.state()
        r, run = self.remove(wt)
        self.assertEqual(r, {"worktree": wt, "branch": "TASK-1-x", "kept": "not pushed"})
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(self.state(), before)
        self.assertEqual({c[6] for c in run.calls}, {wt, agent})

    def test_a_branch_outside_the_prefix_is_kept(self):
        for branch in ("TASK-2-x", "TASK-1-a+b"):
            with self.subTest(branch=branch):
                wt = self.add(branch)
                self.assertEqual(self.remove(wt)[0], {"worktree": wt, "branch": branch, "kept": "not this run's branch"})
                self.assertFalse(os.path.exists(wt))
                self.assertIn(branch, self.branches())

    def test_the_default_branch_is_kept(self):
        git("-C", self.clone, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/TASK-1-remote")
        wt = self.add("TASK-1-remote", "origin/TASK-1-remote")
        self.assertEqual(self.remove(wt)[0], {"worktree": wt, "branch": "TASK-1-remote", "kept": "the default branch"})
        self.assertIn("TASK-1-remote", self.branches())

    def test_a_detached_worktree_deletes_no_branch(self):
        wt = self.add(None)
        before = self.branches()
        r, run = self.remove(wt)
        self.assertEqual(r, {"worktree": wt, "branch": None, "kept": None})
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(self.branches(), before)
        self.assertFalse(any("-D" in c for c in run.calls))

    def test_stale_worktree_entries_are_pruned(self):
        gone, wt = self.add("TASK-2-gone"), self.add()
        shutil.rmtree(gone)
        self.remove(wt)
        self.assertNotIn(gone, git("-C", self.clone, "worktree", "list", "--porcelain"))


class Status(Base):
    def plan_doc(self, branch="TASK-1-x", phase="S9"):
        os.makedirs(os.path.join(self.wt, "docs"), exist_ok=True)
        path = os.path.join(self.wt, "docs", "plan.md")
        with open(path, "w") as f:
            f.write(f"# Plan\n\nRESUME: phase={phase} branch={branch}\n")
        return path

    def test_pr_plan_docs_and_feedback_since_the_latest_plan_doc_commit(self):
        path = self.plan_doc()
        comments = [[{"created_at": "2026-10-01T00:00:00Z", "user": {"login": "me"}, "body": "old"},
                     {"created_at": "2026-10-03T00:00:00Z", "user": {"login": "me"}, "body": "do Y"}]]
        reviews = [[{"submitted_at": "2026-10-03T01:00:00Z", "user": {"login": "bot"}, "body": "nit", "state": "COMMENTED"},
                    {"submitted_at": None, "user": {"login": "bot"}, "body": "pending"}]]
        run = Fake([self.existing(),
                    (lambda a: a[3:5] == ["log", "-1"], ok("2026-10-02T00:00:00+00:00\n")),
                    (["gh", "api", "--hostname", "github.com", "user"], ok(json.dumps({"login": "me"}))),
                    (["gh", "pr", "list"], ok(json.dumps([
                        {"number": 9, "url": "u9", "state": "OPEN", "isCrossRepository": True, "author": {"login": "me"}},
                        {"number": 7, "url": "u7", "state": "OPEN", "isCrossRepository": False, "author": {"login": "me"}}]))),
                    (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                    (lambda a: a[-1].endswith("/pulls/7/reviews"), ok(json.dumps(reviews))),
                    (lambda a: a[-1].endswith("/pulls/7/comments"), ok("[[]]"))])
        code, out, _ = self.main(["status", "o/n", "--branch", "TASK-1-x", "--dir", self.dir], run)
        self.assertEqual(code, 0)
        r = json.loads(out)
        self.assertEqual(r["pr"], {"number": 7, "url": "u7", "state": "OPEN"})
        self.assertEqual(r["plan_docs"], [{"path": path, "phase": "S9"}])
        self.assertEqual([e["body"] for e in r["user"]], ["do Y"])
        self.assertEqual([(e["body"], e["state"]) for e in r["others"]], [("nit", "COMMENTED")])

    def test_configured_users_replace_the_gh_login(self):
        comments = [[{"created_at": "2026-10-03T00:00:00Z", "user": {"login": "me"}, "body": "from the bot"},
                     {"created_at": "2026-10-03T01:00:00Z", "user": {"login": "Alice"}, "body": "do Y"}]]
        run = Fake([self.existing(), (["gh", "api", "--hostname", "github.com", "user"], ok('{"login": "me"}')),
                    (["gh", "pr", "list"], ok(json.dumps([{"number": 7, "url": "u7", "state": "OPEN",
                                                           "isCrossRepository": False, "author": {"login": "me"}}]))),
                    (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                    (lambda a: "--paginate" in a, ok("[[]]"))])
        config = os.path.join(self.dir, "config.toml")
        with open(config, "w") as f:
            f.write('users = ["alice"]\n')
        out, err = io.StringIO(), io.StringIO()
        code = repo.main(["status", "o/n", "--branch", "b", "--dir", self.dir], run=run, out=out, err=err, config=config)
        r = json.loads(out.getvalue())
        self.assertEqual((code, r["pr"]["number"]), (0, 7))
        self.assertEqual([e["body"] for e in r["user"]], ["do Y"])
        self.assertEqual([e["body"] for e in r["others"]], ["from the bot"])

    def test_other_branches_plan_docs_and_no_pr(self):
        self.plan_doc(branch="other")
        run = Fake([self.existing(), (["gh", "api", "--hostname", "github.com", "user"], ok('{"login": "me"}')),
                    (["gh", "pr", "list"], ok("[]"))])
        code, out, _ = self.main(["status", "o/n", "--branch", "TASK-1-x", "--dir", self.dir], run)
        self.assertEqual((code, json.loads(out)), (0, {"pr": None, "plan_docs": [], "since": None, "user": [], "others": []}))

    def test_bad_config_is_reported_before_an_unreadable_repo(self):
        config = os.path.join(self.dir, "config.toml")
        with open(config, "w") as f:
            f.write("users = [\n")
        out, err = io.StringIO(), io.StringIO()
        code = repo.main(["status", "--dir", self.dir, "--branch", "b", "not a repo"], run=Fake([]), out=out, err=err,
                         config=config)
        self.assertEqual(code, 1)

    def test_needs_a_checkout(self):
        code, _, err = self.main(["status", "o/n", "--branch", "b", "--dir", self.dir], Fake([]))
        self.assertEqual(code, 2)
        self.assertIn("run worktree first", err)


if __name__ == "__main__":
    unittest.main()
