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
FETCH = ("fetch", "--refmap=", "origin", "+refs/heads/*:refs/remotes/origin/*")


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


def fail(stderr):
    return subprocess.CompletedProcess([], 1, "", stderr)


class Fake:
    """A `run` that answers by argv prefix (a list answers in turn) and records every call; `gh repo clone` makes the
    checkout's .git."""

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
        self.none = os.path.join(self.dir, "none.toml")

    def existing(self, url=URL):
        os.makedirs(os.path.join(self.wt, ".git"))
        return (lambda a: a[3:] == ["remote", "get-url", "origin"], ok(url + "\n"))

    def main(self, argv, run, config=None):
        out, err = io.StringIO(), io.StringIO()
        code = repo.main(argv, run=run, out=out, err=err, config=config or self.none)
        return code, out.getvalue(), err.getvalue()

    def config(self, text):
        path = os.path.join(self.dir, "config.toml")
        with open(path, "w") as f:
            f.write(text)
        return path


class ReadConfig(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "config.toml")

    def test_merge(self):
        base = {"a": 1, "s": "x", "u": {"k": 1}, "t": {"x": 1, "l": [1, 2], "n": {"p": 1, "q": 2}}}
        over = {"b": 2, "s": {"k": 1}, "u": 5, "t": {"y": 2, "l": [3], "n": {"q": 3}}}
        self.assertEqual(repo.merge(base, over), {"a": 1, "b": 2, "s": {"k": 1}, "u": 5,
                                                  "t": {"x": 1, "y": 2, "l": [3], "n": {"p": 1, "q": 3}}})
        self.assertEqual(base, {"a": 1, "s": "x", "u": {"k": 1}, "t": {"x": 1, "l": [1, 2], "n": {"p": 1, "q": 2}}})

    def test_config_local_toml_beside_goes_on_top(self):
        with open(self.path, "w") as f:
            f.write('users = ["a", "b"]\n[t]\nx = 1\ny = 1\n')
        self.assertEqual(repo.read_config(self.path), {"users": ["a", "b"], "t": {"x": 1, "y": 1}})
        with open(os.path.join(os.path.dirname(self.path), repo.LOCAL), "w") as f:
            f.write('users = ["c"]\n[t]\ny = 2\n[t.n]\nz = 3\n')
        self.assertEqual(repo.read_config(self.path), {"users": ["c"], "t": {"x": 1, "y": 2, "n": {"z": 3}}})


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

    def test_more_url_forms(self):
        for url, want in (("ssh://git@ghe.io:2222/o/n.git", "ghe.io/o/n"), ("ssh://ghe.io/o/n", "ghe.io/o/n"),
                          ("https://user@github.com/o/n", "github.com/o/n"),
                          ("https://user:tok@github.com/o/n.git", "github.com/o/n"),
                          ("http://ghe.io/o/n/", "ghe.io/o/n"), ("https://ghe.io:8443/o/n", "ghe.io:8443/o/n")):
            with self.subTest(url=url):
                self.assertEqual(repo.url_slug(url), want)
                repo.Repo.parse(want)
        for url in ("ftp://ghe.io/o/n", "https://ghe.io/o", "ssh://git@ghe.io:x/o/n", "https://ghe.io/o/n/extra"):
            with self.subTest(url=url):
                self.assertIsNone(repo.url_slug(url))


class ErrText(unittest.TestCase):
    def test_userinfo_is_redacted(self):
        res = fail("fatal: could not read Password for 'https://SECRETTOK@github.com': no\n"
                   "fatal: unable to access 'https://u:SECRET2@ghe.io/o/n/'")
        text = repo.err_text(res)
        self.assertNotIn("SECRET", text)
        self.assertIn("'https://***@github.com'", text)
        self.assertIn("'https://***@ghe.io/o/n/'", text)

    def test_trimmed_and_capped(self):
        for stderr, want in ((None, ""), ("", ""), ("  HTTP 404: Not Found \n", "HTTP 404: Not Found"), ("x" * 300, "x" * 200)):
            with self.subTest(stderr=stderr):
                self.assertEqual(repo.err_text(subprocess.CompletedProcess([], 1, stderr=stderr)), want)


class TempDirs(unittest.TestCase):
    def test_every_temp_dir_counts(self):
        getconf = subprocess.CompletedProcess([], 0, "/x/d/\n", "")
        with unittest.mock.patch.dict(os.environ, {"TMPDIR": "/x/e/"}), \
                unittest.mock.patch("tempfile.gettempdir", return_value="/x/t"), \
                unittest.mock.patch("subprocess.run", return_value=getconf) as sub:
            dirs = repo.temp_dirs()
        self.assertEqual(sub.call_args.args[0], ["getconf", "DARWIN_USER_TEMP_DIR"])
        self.assertEqual(dirs, tuple(dict.fromkeys(os.path.realpath(d) for d in ("/x/e", "/x/t", "/tmp", "/var/tmp", "/x/d"))))

    def test_getconf_failing_is_ignored(self):
        with unittest.mock.patch("subprocess.run", side_effect=OSError("no getconf")):
            self.assertIn(os.path.realpath("/var/tmp"), repo.temp_dirs())


class TrustedDirs(unittest.TestCase):
    def test_unset_is_empty_and_paths_are_real_paths(self):
        self.assertEqual(repo.trusted_dirs({}), frozenset())
        with unittest.mock.patch.dict(os.environ, {"HOME": "/var/h"}):
            got = repo.trusted_dirs({"trusted_dirs": ["~/c", "/x/../y"]})
        self.assertEqual(got, {os.path.realpath("/var/h/c"), os.path.realpath("/y")})


class Worktree(Base):
    def worktree(self, *answers, branch="TASK-1-x", spec="o/n", config=None):
        run = Fake([*answers, HEAD])
        return self.main(["worktree", spec, f"--branch={branch}", "--dir", self.dir], run, config), run

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

    def test_a_remote_repo_gets_no_symlinks_whatever_the_list(self):
        (code, _, err), run = self.worktree(info(), config=self.config(f'trusted_dirs = ["{self.dir}", "~/n"]\n'))
        self.assertEqual(code, 0, err)
        self.assertIn(["gh", "repo", "clone", "github.com/o/n", self.wt, "--", "-c", "core.symlinks=false", "--filter=blob:none"],
                      run.calls)

    def test_a_bad_trusted_dirs_fails_worktree_before_the_repo_is_read(self):
        for text, want in (("trusted_dirs = [\n", "(at "), ('trusted_dirs = "/a"\n', "trusted_dirs"),
                           ("trusted_dirs = [1]\n", "trusted_dirs"), ('trusted_dirs = ["a/b"]\n', "trusted_dirs"),
                           ('trusted_dirs = ["~nosuchuser/x"]\n', "trusted_dirs")):
            with self.subTest(text=text):
                run = Fake([])
                code, _, err = self.main(["worktree", "--dir", self.dir, "--branch", "TASK-1-x", "not a repo"], run,
                                         self.config(text))
                self.assertEqual((code, run.calls), (1, []), err)
                self.assertIn(want, err)

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
        self.assertTrue(run.ran("git", "-C", self.wt, *FETCH))
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

    def test_a_token_in_git_output_is_redacted(self):
        code, _, err = self.main(["worktree", "o/n", "--branch=TASK-1-x", "--dir", self.dir],
                                 Fake([self.existing(), info(), (lambda a: "fetch" in a, fail("fatal: could not read Password for 'https://TOK@github.com'"))]))
        self.assertEqual(code, 1)
        self.assertNotIn("TOK", err)
        self.assertIn("https://***@github.com", err)

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
                    repo.main([cmd, *argv], run=Fake([]), config=self.none)
                self.assertIn(f"{opt} given twice", err.getvalue())

    def test_branch_is_required(self):
        for cmd in ("worktree", "status"):
            with self.subTest(cmd=cmd), self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr", io.StringIO()):
                repo.main([cmd, "--dir", self.dir, "o/n"], run=Fake([]), config=self.none)

    def test_prepare_and_checkout_are_gone(self):
        for cmd in ("prepare", "checkout"):
            with self.subTest(cmd=cmd), self.assertRaises(SystemExit), unittest.mock.patch("sys.stderr", io.StringIO()):
                repo.main([cmd, "--dir", self.dir, "--branch", "b", "o/n"], run=Fake([]), config=self.none)


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

    def test_a_long_path_keeps_the_subcommand_whole(self):
        run = Fake([(["git"], [fail("fatal: boom")])])
        with self.assertRaises(RuntimeError) as cm:
            repo.git(run, "/w", "--git-dir=" + "/private/var/folders/xy" * 10, "worktree", "prune")
        msg = str(cm.exception)
        self.assertTrue(msg.startswith("git --git-dir=/private/var/folders/xy"), msg)
        self.assertTrue(msg.endswith("worktree prune: fatal: boom"), msg)
        self.assertLess(len(msg), 120)

    def test_the_command_text_stays_bounded(self):
        run = Fake([(["git"], [fail("fatal: boom")])])
        with self.assertRaises(RuntimeError) as cm:
            repo.git(run, "/w", *["/a/long/path/argument" * 3] * 50)
        self.assertLess(len(str(cm.exception)), 250)

    def test_other_failures_are_not_retried(self):
        err, run, sleep = self.git(fail("fatal: not a git repository"), ok())
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(len(run.calls), 1)
        sleep.assert_not_called()


class Clone(unittest.TestCase):
    """A real clone of a bare repo, its origin reading as github.com/o/n."""

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
        self.none = os.path.join(self.tmp, "none.toml")

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
    def worktree(self, branch="TASK-1-x", spec=None, base=None, config=None):
        run, out, err = Real([info()]), io.StringIO(), io.StringIO()
        code = repo.main(["worktree", "--dir", base or self.dir, "--branch", branch, spec or self.clone], run=run, out=out, err=err,
                         config=config or self.none, temp=())
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
                results[branch] = repo.worktree(self.clone, branch, os.path.join(self.tmp, branch), run=Real([info()]), temp=())
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
                         config=self.none)
        self.assertEqual(code, 0, err.getvalue())
        self.assertIsNone(json.loads(out.getvalue())["pr"])
        self.assertTrue(run.ran("gh", "pr", "list", "--repo", "github.com/o/n"))


class LocalSafety(Clone):
    def worktree(self, branch="TASK-1-x", fail_on=None, temp=(), config=None, spec=None):
        run, out, err = Real([info(), *([(fail_on, fail("boom"))] if fail_on else [])]), io.StringIO(), io.StringIO()
        code = repo.main(["worktree", "--dir", self.dir, "--branch", branch, spec or self.clone], run=run, out=out, err=err,
                         config=config or self.none, temp=temp)
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
        self.assertEqual(git("-C", self.wt, "config", "--worktree", "--get", "core.symlinks"), "false")

    def test_a_local_clone_under_a_temp_dir_is_refused(self):
        code, err = self.worktree(temp=(self.tmp,))
        self.assertEqual(code, 2)
        self.assertIn("temp dir", err)
        self.assertFalse(os.path.exists(self.wt))

    def test_a_failed_step_after_the_add_leaves_no_worktree_and_a_rerun_succeeds(self):
        self.file_commit()
        for step, fail_on in (("config", lambda a: a[3:5] == ["config", "--worktree"] and "--get" not in a),
                              ("reset", lambda a: a[3:4] == ["reset"])):
            with self.subTest(step):
                code, err = self.worktree(fail_on=fail_on)
                self.assertEqual(code, 1, err)
                self.assertFalse(os.path.exists(self.wt))
                self.assertNotIn(self.wt, git("-C", self.clone, "worktree", "list", "--porcelain"))
                self.assertEqual(self.worktree(), (0, ""))
                self.assertGood()
                shutil.rmtree(self.wt)
                git("-C", self.clone, "worktree", "prune")

    def test_a_half_made_worktree_is_made_again(self):
        self.file_commit()
        git("-C", self.clone, "config", "extensions.worktreeConfig", "true")
        git("-C", self.clone, "worktree", "add", "-q", "--no-checkout", "-b", "TASK-1-x", self.wt, "origin/main")
        self.assertEqual(self.worktree(), (0, ""))
        self.assertGood()

    def test_a_half_made_worktree_of_a_listed_clone_is_made_again_with_symlinks(self):
        link = self.link_commit()
        git("-C", self.clone, "fetch", "-q")
        git("-C", self.clone, "config", "extensions.worktreeConfig", "true")
        git("-C", self.clone, "worktree", "add", "-q", "--no-checkout", "-b", "TASK-1-x", self.wt, "origin/main")
        self.assertEqual(self.worktree(config=self.listing(self.clone)), (0, ""))
        self.assertEqual(git("-C", self.wt, "config", "--worktree", "--get", "core.symlinks"), "true")
        self.assertEqual(os.readlink(link), "/etc/passwd")

    def test_a_worktree_with_symlinks_on_is_refused(self):
        self.file_commit()
        git("-C", self.clone, "worktree", "add", "-q", "-b", "TASK-1-x", self.wt, "origin/main")
        code, err = self.worktree()
        self.assertEqual(code, 2)
        self.assertIn("symlinks", err)
        self.assertIn("unset", err)

    def test_a_worktree_gets_no_symlinks_and_the_clone_keeps_its_config(self):
        link = self.link_commit()
        self.assertEqual(self.worktree()[0], 0)
        self.assertTrue(os.path.isfile(link) and not os.path.islink(link))
        self.assertEqual(git("-C", self.wt, "status", "--porcelain"), "")
        self.assertEqual(git("-C", self.wt, "config", "--get", "core.symlinks"), "false")
        self.assertEqual(subprocess.run(["git", "-C", self.clone, "config", "--get", "core.symlinks"]).returncode, 1)
        self.assertEqual(git("-C", self.clone, "status", "--porcelain"), "")

    def test_a_listed_clones_worktree_gets_symlinks_and_the_clone_keeps_its_config(self):
        link = self.link_commit()
        self.assertEqual(self.worktree(config=self.listing(self.clone)), (0, ""))
        self.assertEqual(os.readlink(link), "/etc/passwd")
        self.assertEqual(git("-C", self.wt, "config", "--worktree", "--get", "core.symlinks"), "true")
        self.assertEqual(git("-C", self.wt, "status", "--porcelain"), "")
        self.assertEqual(subprocess.run(["git", "-C", self.clone, "config", "--get", "core.symlinks"]).returncode, 1)

    def test_a_clone_listed_in_config_local_toml_gets_symlinks(self):
        link = self.link_commit()
        config = self.listing()
        with open(os.path.join(self.tmp, "config.local.toml"), "w") as f:
            f.write(f"trusted_dirs = {json.dumps([self.clone])}\n")
        self.assertEqual(self.worktree(config=config), (0, ""))
        self.assertEqual(os.readlink(link), "/etc/passwd")

    def test_a_listed_clone_is_matched_by_real_path(self):
        link = self.link_commit()
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.clone, alias)
        for i, (listed, spec) in enumerate(((alias, self.clone), (self.clone, alias))):
            with self.subTest(listed=listed, spec=spec):
                try:
                    self.assertEqual(self.worktree(f"TASK-1-{i}", config=self.listing(listed), spec=spec), (0, ""))
                    self.assertTrue(os.path.islink(link))
                finally:
                    shutil.rmtree(self.wt, ignore_errors=True)
                    git("-C", self.clone, "worktree", "prune")

    def test_listing_another_clone_of_the_same_origin_trusts_no_other(self):
        link = self.link_commit()
        other = os.path.join(self.tmp, "clone2")
        git("clone", "-q", self.bare, other)
        self.assertEqual(self.worktree(config=self.listing(other)), (0, ""))
        self.assertTrue(os.path.isfile(link) and not os.path.islink(link))
        self.assertEqual(git("-C", self.wt, "config", "--worktree", "--get", "core.symlinks"), "false")

    def test_an_existing_worktree_must_have_the_listed_setting(self):
        self.file_commit()
        listed = self.listing(self.clone)
        self.assertEqual(self.worktree(), (0, ""))
        code, err = self.worktree(config=listed)
        self.assertEqual(code, 2)
        for text in ("symlinks", "false", "true", "trusted_dirs", "remove it and run again"):
            self.assertIn(text, err)
        self.assertEqual(git("-C", self.wt, "config", "--worktree", "--get", "core.symlinks"), "false")
        shutil.rmtree(self.wt)
        git("-C", self.clone, "worktree", "prune")
        self.assertEqual(self.worktree(config=listed), (0, ""))
        self.assertEqual(self.worktree(config=listed), (0, ""))

    def test_a_worktree_made_while_listed_is_refused_once_unlisted(self):
        self.file_commit()
        self.assertEqual(self.worktree(config=self.listing(self.clone)), (0, ""))
        code, err = self.worktree()
        self.assertEqual(code, 2)
        self.assertIn("symlinks", err)

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


class CommonDir(Clone):
    def add(self, branch="TASK-1-x", opts=()):
        wt = os.path.join(self.tmp, "w", branch)
        git("-C", self.clone, "worktree", "add", "-q", *opts, "-b", branch, wt)
        return wt

    def test_a_worktree_names_its_clones_git_dir(self):
        common = os.path.join(self.clone, ".git")
        self.assertEqual(repo.common_dir(self.add()), common)
        self.assertEqual(repo.common_dir(self.add("TASK-1-rel", ("--relative-paths",))), common)

    def test_a_crlf_git_file_is_read_as_git_reads_it(self):
        wt = self.add()
        gitdir = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        with open(os.path.join(wt, ".git"), "w", newline="") as f:
            f.write(f"gitdir: {gitdir}\r\n")
        self.assertEqual(repo.common_dir(wt), os.path.join(self.clone, ".git"))

    def test_trailing_whitespace_is_part_of_the_path_as_for_git(self):
        wt = self.add()
        admin = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        fake, other = os.path.join(self.tmp, "fake-admin"), os.path.join(self.tmp, "other", ".git")
        os.makedirs(fake)
        os.makedirs(other)
        with open(os.path.join(fake, "commondir"), "w") as f:
            f.write(other + "\n")
        a = os.path.join(self.tmp, "a")
        os.makedirs(a)
        os.symlink(admin, os.path.join(a, "sym"))
        os.symlink(fake, os.path.join(a, "sym "))
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write(f"gitdir: {a}/sym \n")
        self.assertEqual(repo.common_dir(wt), other)

    def test_no_worktree_names_no_clone(self):
        wt = self.add()
        plain, linked, junk, orphan, bare = (os.path.join(self.tmp, d) for d in ("plain", "linked", "junk", "orphan", "bare"))
        for d in (plain, linked, junk, orphan, bare):
            os.makedirs(d)
        os.symlink(os.path.join(wt, ".git"), os.path.join(linked, ".git"))
        for d, text in ((junk, "nonsense\n"), (orphan, f"gitdir: {self.tmp}/gone\n"), (bare, f"gitdir: {self.bare}\n")):
            with open(os.path.join(d, ".git"), "w") as f:
                f.write(text)
        for path in (self.clone, plain, linked, junk, orphan, bare, os.path.join(self.tmp, "missing")):
            with self.subTest(path=path):
                self.assertIsNone(repo.common_dir(path))


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
        config, local = os.path.join(self.dir, "config.toml"), os.path.join(self.dir, "config.local.toml")
        existing = self.existing()
        for text, local_text in (('users = ["alice"]\n', None), ('users = ["bob"]\n', 'users = ["alice"]\n')):
            with self.subTest(local=local_text):
                run = Fake([existing, (["gh", "api", "--hostname", "github.com", "user"], ok('{"login": "me"}')),
                            (["gh", "pr", "list"], ok(json.dumps([{"number": 7, "url": "u7", "state": "OPEN",
                                                                   "isCrossRepository": False, "author": {"login": "me"}}]))),
                            (lambda a: a[-1].endswith("/issues/7/comments"), ok(json.dumps(comments))),
                            (lambda a: "--paginate" in a, ok("[[]]"))])
                with open(config, "w") as f:
                    f.write(text)
                if local_text:
                    with open(local, "w") as f:
                        f.write(local_text)
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

    def test_an_invalid_trusted_dirs_does_not_affect_status(self):
        run = Fake([self.existing(), (["gh", "api", "--hostname", "github.com", "user"], ok('{"login": "me"}')),
                    (["gh", "pr", "list"], ok("[]"))])
        code, _, err = self.main(["status", "o/n", "--branch", "TASK-1-x", "--dir", self.dir], run,
                                 self.config('trusted_dirs = ["a/b"]\n'))
        self.assertEqual(code, 0, err)

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
