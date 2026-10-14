"""utils/src/agent_pm/utils/git.py: common_dir over real git worktrees; git's lock retries."""

import os
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace

import agent_pm.utils.git
from agent_pm.utils import git as repos
from agent_pm.utils.git import LOCAL_ENV_VARS, Checkouts, find_checkouts

from utils.tests.case import Case
from utils.tests.process_fixtures import TimeoutExpired


def ok(stdout=""):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def fail(stderr):
    return SimpleNamespace(returncode=1, stdout="", stderr=stderr)


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
                return agent_pm.utils.git.git("/w", *args, run=run), calls, sleep
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
            "/w",
            "status",
            run=lambda argv, t: seen.append((argv, t)) or ok(),
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


class CheckoutSearch(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.inner = os.path.join(self.root, "inner")
        os.mkdir(self.inner)
        self.calls = []

    def runner(self, argv, timeout, env):
        self.calls.append((argv, timeout, env))
        current = argv[argv.index("-C") + 1]
        if current in (self.root, self.inner):
            return ok(f"true\n{current}\n")
        return SimpleNamespace(
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository (or any of the parent directories): .git\n",
        )

    def test_collects_inner_then_outer_and_uses_the_existing_ancestor(self):
        result = find_checkouts(
            os.path.join(self.inner, "missing", "child"), runner=self.runner, timeout=7
        )
        self.assertEqual(result, Checkouts((self.inner, self.root)))
        self.assertEqual(
            self.calls[0][0][-4:],
            [self.inner, "rev-parse", "--is-inside-work-tree", "--show-toplevel"],
        )
        self.assertTrue(all(timeout == 7 for _, timeout, _ in self.calls))
        self.assertTrue(all(argv[1:5] == list(repos.GUARD) for argv, _, _ in self.calls))

    def test_repository_selection_variables_are_removed_without_changing_the_parent(self):
        supplied = dict.fromkeys(LOCAL_ENV_VARS, "poison") | {"GIT_EDITOR": "keep"}
        with unittest.mock.patch.dict(os.environ, supplied):
            self.assertIsNone(find_checkouts(self.inner, runner=self.runner, timeout=7).error)
            for _, _, env in self.calls:
                self.assertFalse(LOCAL_ENV_VARS & env.keys())
                self.assertEqual((env["GIT_EDITOR"], env["LC_ALL"]), ("keep", "C"))
            self.assertEqual({key: os.environ[key] for key in supplied}, supplied)

    def test_no_checkout_is_distinct_from_failure_and_partial_success_is_preserved(self):
        absent = SimpleNamespace(
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository (or any of the parent directories): .git\n",
        )
        self.assertEqual(
            find_checkouts(self.inner, runner=lambda *_args: absent, timeout=7), Checkouts(())
        )
        for message in (
            "permission denied",
            "fatal: detected dubious ownership in repository",
            "fatal: invalid gitfile format",
            "fatal: unknown failure",
        ):

            def failed(argv, timeout, env, message=message):
                if argv[argv.index("-C") + 1] == self.inner:
                    return self.runner(argv, timeout, env)
                return SimpleNamespace(returncode=128, stdout="", stderr=message)

            with self.subTest(message=message):
                result = find_checkouts(self.inner, runner=failed, timeout=7)
                self.assertEqual(result.roots, (self.inner,))
                self.assertIn(message, result.error)

    def test_timeouts_and_operating_system_errors_are_failures(self):
        for error in (TimeoutExpired(["git"], 7), PermissionError("denied")):

            def failed(_argv, _timeout, _env, error=error):
                raise error

            with self.subTest(error=error):
                result = find_checkouts(self.inner, runner=failed, timeout=7)
                self.assertEqual(result.roots, ())
                self.assertIn(str(error), result.error)

    def test_corrupt_metadata_cannot_be_classified_as_no_checkout(self):
        os.mkdir(os.path.join(self.inner, ".git"))
        absent = SimpleNamespace(
            returncode=128,
            stdout="",
            stderr="fatal: not a git repository (or any of the parent directories): .git\n",
        )
        result = find_checkouts(self.inner, runner=lambda *_args: absent, timeout=7)
        self.assertEqual(result.roots, ())
        self.assertIn("not a git repository", result.error)

    def test_bad_git_output_is_an_error(self):
        for output in ("", "true\nrelative\n", "false\n"):
            with self.subTest(output=output):
                result = find_checkouts(
                    self.inner, runner=lambda *_args, output=output: ok(output), timeout=7
                )
                self.assertIn("invalid git rev-parse output", result.error)

    def test_case_aliases_accept_metadata_and_deduplicate_by_filesystem_identity(self):
        os.mkdir(os.path.join(self.inner, ".git"))
        os.mkdir(os.path.join(self.inner, "child"))
        variant = os.path.join(self.root, "INNER")
        native_stat, native_lstat = os.stat, os.lstat

        def case_stat(path, *args, **kwargs):
            return native_stat(str(path).replace(variant, self.inner, 1), *args, **kwargs)

        def case_lstat(path, *args, **kwargs):
            return native_lstat(str(path).replace(variant, self.inner, 1), *args, **kwargs)

        def runner(argv, _timeout, _env):
            current = argv[argv.index("-C") + 1]
            if current == os.path.join(variant, "child"):
                return ok(f"true\n{variant}\n")
            if current == variant:
                return ok(f"true\n{self.inner}\n")
            return self.runner(argv, _timeout, _env)

        with (
            unittest.mock.patch("os.stat", side_effect=case_stat),
            unittest.mock.patch("os.lstat", side_effect=case_lstat),
        ):
            result = find_checkouts(os.path.join(variant, "child"), runner=runner, timeout=7)
        self.assertEqual(result, Checkouts((variant, self.root)))
