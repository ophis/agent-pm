"""utils/src/agent_pm/utils/git.py: common_dir over real git worktrees; git's lock retries."""

import os
import subprocess
import tempfile
import unittest
import unittest.mock

import agent_pm.utils.git
from agent_pm.utils import git as repos
from agent_pm.utils.git import common_dir

from utils.tests.case import Case


def git(*argv):
    return subprocess.run(["git", *argv], check=True, capture_output=True, text=True).stdout.strip()


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


def fail(stderr):
    return subprocess.CompletedProcess([], 1, "", stderr)


class CommonDir(Case):
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
        self.clone, self.bare = (
            os.path.join(self.tmp, "clone"),
            os.path.join(self.tmp, "origin.git"),
        )
        git("init", "-q", "-b", "main", self.clone)
        git("-C", self.clone, "commit", "-q", "--allow-empty", "-m", "one")
        git("clone", "-q", "--bare", self.clone, self.bare)

    def add(self, branch="TASK-1-x", opts=()):
        wt = os.path.join(self.tmp, "w", branch)
        git("-C", self.clone, "worktree", "add", "-q", *opts, "-b", branch, wt)
        return wt

    def test_a_worktree_names_its_clones_git_dir(self):
        common = os.path.join(self.clone, ".git")
        self.assertEqual(common_dir(self.add()), common)
        self.assertEqual(common_dir(self.add("TASK-1-rel", ("--relative-paths",))), common)

    def test_a_crlf_git_file_is_read_as_git_reads_it(self):
        wt = self.add()
        gitdir = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        with open(os.path.join(wt, ".git"), "w", newline="") as f:
            f.write(f"gitdir: {gitdir}\r\n")
        self.assertEqual(common_dir(wt), os.path.join(self.clone, ".git"))

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
        self.assertEqual(common_dir(wt), other)

    def test_a_commondir_symlink_is_followed(self):
        wt = self.add()
        admin = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        path = os.path.join(admin, "commondir")
        target = path + "-target"
        os.rename(path, target)
        os.symlink(target, path)
        self.assertEqual(common_dir(wt), os.path.join(self.clone, ".git"))

    def test_a_file_replaced_by_a_fifo_at_open_is_rejected_without_waiting(self):
        wt = self.add()
        admin = git("-C", wt, "rev-parse", "--path-format=absolute", "--git-dir")
        open_file = os.open
        for path in (os.path.join(wt, ".git"), os.path.join(admin, "commondir")):
            with self.subTest(path=path):
                backup = path + "-backup"

                def replace(file, flags, *args, target=path, **kwargs):
                    if file == target:
                        self.assertTrue(flags & os.O_NONBLOCK)
                        os.rename(target, target + "-backup")
                        os.mkfifo(target)
                    return open_file(file, flags, *args, **kwargs)

                with unittest.mock.patch("os.open", side_effect=replace):
                    self.assertIsNone(common_dir(wt))
                os.unlink(path)
                os.rename(backup, path)

    def test_a_git_file_replaced_by_a_symlink_at_open_is_rejected(self):
        wt = self.add()
        path = os.path.join(wt, ".git")
        target = path + "-target"
        open_file = os.open

        def replace(file, flags, *args, **kwargs):
            if file == path:
                os.rename(path, target)
                os.symlink(target, path)
            return open_file(file, flags, *args, **kwargs)

        with unittest.mock.patch("os.open", side_effect=replace):
            self.assertIsNone(common_dir(wt))

    def test_no_worktree_names_no_clone(self):
        wt = self.add()
        plain, linked, junk, orphan, bare = (
            os.path.join(self.tmp, d) for d in ("plain", "linked", "junk", "orphan", "bare")
        )
        for d in (plain, linked, junk, orphan, bare):
            os.makedirs(d)
        os.symlink(os.path.join(wt, ".git"), os.path.join(linked, ".git"))
        for d, text in (
            (junk, "nonsense\n"),
            (orphan, f"gitdir: {self.tmp}/gone\n"),
            (bare, f"gitdir: {self.bare}\n"),
        ):
            with open(os.path.join(d, ".git"), "w") as f:
                f.write(text)
        for path in (
            self.clone,
            plain,
            linked,
            junk,
            orphan,
            bare,
            os.path.join(self.tmp, "missing"),
        ):
            with self.subTest(path=path):
                self.assertIsNone(common_dir(path))


class Retry(Case):
    LOCKS = (
        "fatal: Unable to create '/w/.git/index.lock': File exists.",
        "error: cannot lock ref 'refs/remotes/origin/main': is at abc but expected def",
        "error: could not lock config file .git/config: File exists",
    )

    def git(self, *results, args=("fetch", "origin")):
        """(stdout or the RuntimeError, the argvs run, the mocked sleep)."""
        calls, answers = [], list(results)

        def run(argv, _timeout):
            calls.append(argv)
            return answers.pop(0)

        with unittest.mock.patch.object(agent_pm.utils.git.time, "sleep") as sleep:
            try:
                return agent_pm.utils.git.git(run, "/w", *args), calls, sleep
            except RuntimeError as e:
                return e, calls, sleep

    def test_a_lock_failure_is_retried(self):
        for stderr in self.LOCKS:
            with self.subTest(stderr=stderr):
                out, calls, sleep = self.git(fail(stderr), ok("done"))
                self.assertEqual(
                    (out, calls), ("done", [["git", "-C", "/w", "fetch", "origin"]] * 2)
                )
                sleep.assert_called_once_with(0.5)

    def test_five_lock_failures_raise(self):
        err, calls, sleep = self.git(*[fail(self.LOCKS[0])] * 5)
        self.assertIsInstance(err, RuntimeError)
        self.assertIn("index.lock", str(err))
        self.assertEqual(len(calls), 5)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.5, 1, 2, 4])

    def test_the_command_text_shortens_long_arguments_and_stays_bounded(self):
        msg = str(
            self.git(
                fail("fatal: boom"),
                args=("--git-dir=" + "/private/var/folders/xy" * 10, "worktree", "prune"),
            )[0]
        )
        self.assertTrue(msg.startswith("git --git-dir=/private/var/folders/xy"), msg)
        self.assertTrue(msg.endswith("worktree prune: fatal: boom"), msg)
        self.assertLess(len(msg), 120)
        self.assertLess(
            len(str(self.git(fail("fatal: boom"), args=["/a/long/path/argument" * 3] * 50)[0])), 250
        )

    def test_other_failures_are_not_retried(self):
        err, calls, sleep = self.git(fail("fatal: not a git repository"), ok())
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_pre_options_go_before_the_worktree_and_the_timeout_is_passed(self):
        seen = []
        agent_pm.utils.git.git(
            lambda argv, t: seen.append((argv, t)) or ok(),
            "/w",
            "status",
            timeout=9,
            pre=("-c", "a=b"),
        )
        self.assertEqual(seen, [(["git", "-c", "a=b", "-C", "/w", "status"], 9)])


class Parse(Case):
    def test_forms(self):
        for spec, want in (
            ("o/n", ("github.com", "o", "n")),
            ("ghe.example.com/o/n", ("ghe.example.com", "o", "n")),
            ("https://github.com/o/n.git", ("github.com", "o", "n")),
            ("https://GHE.io/o/n/", ("ghe.io", "o", "n")),
        ):
            self.assertEqual(repos.Repo.parse(spec), repos.Repo(*want), spec)

    def test_unreadable(self):
        for spec in ("n", "o/..", "https://o/n", "o/n; rm -rf /", "-o/n"):
            with self.assertRaises(repos.Invalid, msg=spec):
                repos.Repo.parse(spec)

    def test_url_forms(self):
        """url_slug keeps the case as written; origin lowercases it."""
        for url, want in (
            ("git@GitHub.com:O/N.git", "GitHub.com/O/N"),
            ("https://ghe.io/o/n", "ghe.io/o/n"),
            ("ssh://git@ghe.io:2222/o/n.git", "ghe.io/o/n"),
            ("ssh://ghe.io/o/n", "ghe.io/o/n"),
            ("https://user@github.com/o/n", "github.com/o/n"),
            ("https://user:tok@github.com/o/n.git", "github.com/o/n"),
            ("http://ghe.io/o/n/", "ghe.io/o/n"),
            ("https://ghe.io:8443/o/n", "ghe.io:8443/o/n"),
        ):
            with self.subTest(url=url):
                self.assertEqual(repos.url_slug(url), want)
                self.assertEqual(repos.origin(url), want.lower())
                repos.Repo.parse(want)
        for url in (
            "/tmp/origin.git",
            "ftp://ghe.io/o/n",
            "https://ghe.io/o",
            "ssh://git@ghe.io:x/o/n",
            "https://ghe.io/o/n/extra",
        ):
            with self.subTest(url=url):
                self.assertEqual((repos.url_slug(url), repos.origin(url)), (None, None))


class Checkout(Case):
    def test_checkout_is_the_path_rule(self):
        self.assertEqual(repos.checkout("/b", "o", "n", "TASK-1"), "/b/o/n-TASK-1")
        self.assertEqual(
            repos.checkout("b", "o", "n", "x.y_z-1"),
            os.path.join(os.getcwd(), "b", "o", "n-x.y_z-1"),
        )
        with self.assertRaises(repos.Invalid):
            repos.checkout("/b", "o", "n", "a/b")


if __name__ == "__main__":
    unittest.main()
