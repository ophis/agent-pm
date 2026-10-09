"""fake_gh.py run as a process: the `gh` shim on PATH, and real git reading a bare repo through the gitconfig."""
import json
import os
import subprocess
import tempfile
import unittest

import fake_gh

REPO = {"full_name": "acme/widgets", "default_branch": "main", "permissions": {"push": True}}


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.home, self.bin, self.repos = (os.path.join(self.root, d) for d in ("home", "bin", "repos"))
        for d in (self.home, self.bin, self.repos):
            os.mkdir(d)
        self.scenario = os.path.join(self.root, "scenario.json")
        self.env = {"HOME": self.home, "PATH": f"{self.bin}:/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_ALLOW_PROTOCOL": "file", fake_gh.ENV: self.scenario}
        fake_gh.install(self.bin)

    def write(self, scenario):
        with open(self.scenario, "w") as f:
            json.dump(scenario, f)

    def run_cmd(self, *argv, env=None):
        return subprocess.run(argv, env=self.env if env is None else env, capture_output=True, text=True, timeout=30)

    def gh(self, *argv, **kw):
        return self.run_cmd("gh", *argv, **kw)


class Shim(Case):
    def test_install_writes_an_executable_gh(self):
        path = os.path.join(self.bin, "gh")
        self.assertTrue(os.access(path, os.X_OK))
        self.assertEqual(fake_gh.install(self.bin), path)

    def test_api_prints_the_scenarios_object(self):
        self.write({"repos": {"acme/widgets": REPO, "acme/other": {"full_name": "acme/other"}}})
        res = self.gh("api", "repos/acme/widgets")
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertEqual(json.loads(res.stdout), REPO)

    def test_an_unlisted_repo_is_a_404_on_stderr(self):
        self.write({"repos": {"acme/widgets": REPO}})
        for slug in ("acme/nope", "other/widgets", "Acme/widgets"):
            with self.subTest(slug):
                res = self.gh("api", f"repos/{slug}")
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("HTTP 404", res.stderr)

    def test_any_other_argv_is_a_usage_error(self):
        self.write({"repos": {"acme/widgets": REPO}})
        for argv in ((), ("api",), ("auth", "status"), ("api", "user"), ("api", "repos/acme"),
                     ("api", "repos/acme/widgets/branches"), ("api", "repos/acme/widgets", "--jq", ".name"),
                     ("api", "/repos/acme/widgets"), ("repo", "view", "acme/widgets")):
            with self.subTest(argv):
                res = self.gh(*argv)
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("fake_gh.py:", res.stderr)
                self.assertNotIn("HTTP", res.stderr)

    def test_a_malformed_scenario_is_an_error(self):
        for name, body in {"not json": "{", "not an object": "[]", "no repos": "{}",
                           "unknown key": '{"repos": {}, "orgs": {}}', "repos not an object": '{"repos": []}',
                           "repo not an object": '{"repos": {"acme/widgets": 1}}'}.items():
            with self.subTest(name):
                with open(self.scenario, "w") as f:
                    f.write(body)
                res = self.gh("api", "repos/acme/widgets")
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("fake_gh.py:", res.stderr)
                self.assertNotIn("HTTP", res.stderr)

    def test_a_missing_or_unreadable_scenario_is_an_error(self):
        for name, setup in {"missing": lambda: None, "a directory": lambda: os.mkdir(self.scenario)}.items():
            with self.subTest(name):
                setup()
                res = self.gh("api", "repos/acme/widgets")
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn(f"fake_gh.py: {fake_gh.ENV}:", res.stderr)
                self.assertNotIn("HTTP", res.stderr)

    def test_an_unset_scenario_is_an_error(self):
        env = {k: v for k, v in self.env.items() if k != fake_gh.ENV}
        res = self.gh("api", "repos/acme/widgets", env=env)
        self.assertEqual((res.returncode, res.stdout), (1, ""))
        self.assertIn(f"fake_gh.py: {fake_gh.ENV} is unset", res.stderr)


class Bare(Case):
    def refs(self, path):
        res = self.run_cmd("git", "--git-dir", path, "for-each-ref", "--format=%(refname)")
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout.split()

    def test_bare_makes_the_repo_at_owner_name_dot_git(self):
        path = fake_gh.bare(self.repos, "acme", "widgets", ["TASK-7-x", "feature/y"])
        self.assertEqual(path, os.path.join(self.repos, "acme", "widgets.git"))
        res = self.run_cmd("git", "--git-dir", path, "rev-parse", "--is-bare-repository")
        self.assertEqual(res.stdout.strip(), "true")
        self.assertEqual(sorted(self.refs(path)), ["refs/heads/TASK-7-x", "refs/heads/feature/y"])

    def test_each_branch_is_a_commit(self):
        path = fake_gh.bare(self.repos, "acme", "widgets", ["TASK-7-x"])
        res = self.run_cmd("git", "--git-dir", path, "rev-list", "--count", "TASK-7-x")
        self.assertEqual(res.stdout.strip(), "1")

    def test_no_branches_is_an_empty_repo(self):
        path = fake_gh.bare(self.repos, "acme", "widgets")
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.refs(path), [])

    def test_gitconfig_maps_github_to_the_root(self):
        fake_gh.gitconfig(self.home, self.repos)
        with open(os.path.join(self.home, ".gitconfig")) as f:
            self.assertEqual(f.read(), f'[url "file://{self.repos}/"]\n\tinsteadOf = https://github.com/\n')

    def test_ls_remote_through_the_gitconfig_lists_the_seeded_branch_and_only_it(self):
        fake_gh.bare(self.repos, "acme", "widgets", ["TASK-7-x", "TASK-8-y", "main"])
        fake_gh.bare(self.repos, "acme", "other", ["TASK-7-z"])
        fake_gh.gitconfig(self.home, self.repos)
        res = self.run_cmd("git", "ls-remote", "--heads", "https://github.com/acme/widgets.git", "TASK-7-*")
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertEqual([line.split("\t")[1] for line in res.stdout.splitlines()], ["refs/heads/TASK-7-x"])

    def test_ls_remote_of_a_repo_without_the_branch_is_empty(self):
        fake_gh.bare(self.repos, "acme", "widgets")
        fake_gh.gitconfig(self.home, self.repos)
        res = self.run_cmd("git", "ls-remote", "--heads", "https://github.com/acme/widgets.git", "TASK-7-*")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_targets_ls_remote_argv_works(self):
        fake_gh.bare(self.repos, "acme", "widgets", ["TASK-7-x"])
        fake_gh.gitconfig(self.home, self.repos)
        res = self.run_cmd("git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                           "ls-remote", "--heads", "https://github.com/acme/widgets.git", "TASK-7-*")
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertIn("refs/heads/TASK-7-x", res.stdout)


if __name__ == "__main__":
    unittest.main()
