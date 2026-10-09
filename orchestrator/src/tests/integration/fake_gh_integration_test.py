"""fake_gh.py run as a process: the `gh` shim on PATH, and real git reading a bare repo through the gitconfig."""
import json
import os
import subprocess
import tempfile
import unittest

import fake_gh

REPO = {"full_name": "acme/widgets", "default_branch": "main", "permissions": {"push": True}}
RAW = "Accept: application/vnd.github.raw+json"
PRD = "Product Design/2026-10-09-prd.md"
CONTENTS = {"acme/docs": {
    "main": {"Research/2026-10-09-TASK-7-x.md": "# R\nbody\n", "Research/2026-10-08-TASK-6-y.md": "older",
             "Research/notes/deep.md": "deep", PRD: "caf\u00e9\r\nlast line, no newline", "README.md": "readme"},
    "feature/x": {"Research/only-here.md": "on a branch"}},
    "acme/other": {"main": {"a.md": "other repo"}}}


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


class Contents(Case):
    def setUp(self):
        super().setUp()
        self.write({"repos": {"acme/widgets": REPO}, "contents": CONTENTS})

    def listing(self, *argv):
        res = self.gh("api", *argv)
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        return json.loads(res.stdout)

    def test_a_dir_is_a_sorted_list_of_its_direct_entries(self):
        want = [{"name": "2026-10-08-TASK-6-y.md", "path": "Research/2026-10-08-TASK-6-y.md", "type": "file"},
                {"name": "2026-10-09-TASK-7-x.md", "path": "Research/2026-10-09-TASK-7-x.md", "type": "file"},
                {"name": "notes", "path": "Research/notes", "type": "dir"}]
        self.assertEqual(self.listing("repos/acme/docs/contents/Research?ref=main"), want)
        self.assertEqual(self.listing("repos/acme/docs/contents/Research/notes?ref=main"),
                         [{"name": "deep.md", "path": "Research/notes/deep.md", "type": "file"}])

    def test_a_dir_with_raw_is_the_same_list(self):
        url = "repos/acme/docs/contents/Research?ref=main"
        self.assertEqual(self.listing("-H", RAW, url), self.listing(url))

    def test_raw_prints_the_file_verbatim(self):
        url = "repos/acme/docs/contents/Product%20Design/2026-10-09-prd.md?ref=main"
        res = subprocess.run(["gh", "api", "-H", RAW, url], env=self.env, capture_output=True, timeout=30)
        text = CONTENTS["acme/docs"]["main"][PRD]
        self.assertEqual((res.returncode, res.stderr, res.stdout), (0, b"", text.encode()))

    def test_a_file_without_the_header_is_its_entry(self):
        self.assertEqual(self.listing("repos/acme/docs/contents/Product%20Design/2026-10-09-prd.md?ref=main"),
                         {"name": "2026-10-09-prd.md", "path": PRD, "type": "file"})
        self.assertEqual(self.listing("repos/acme/docs/contents/README.md?ref=main"),
                         {"name": "README.md", "path": "README.md", "type": "file"})

    def test_a_quoted_path_and_ref_are_decoded(self):
        self.assertEqual(self.listing("repos/acme/docs/contents/Product%20Design?ref=main"),
                         [{"name": "2026-10-09-prd.md", "path": PRD, "type": "file"}])
        res = self.gh("api", "-H", RAW, "repos/acme/docs/contents/Research/only-here.md?ref=feature%2Fx")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "on a branch", ""))

    def test_repos_is_unchanged_beside_contents(self):
        self.assertEqual(self.gh("api", "repos/acme/widgets").stdout, json.dumps(REPO) + "\n")
        res = self.gh("api", "-H", RAW, "repos/acme/other/contents/a.md?ref=main")
        self.assertEqual((res.returncode, res.stdout), (0, "other repo"))

    def test_a_missing_path_branch_or_repo_is_a_404_on_stderr(self):
        docs = "repos/acme/docs/contents/"
        for url in (docs + "Nope?ref=main", docs + "Research/missing.md?ref=main", docs + "Resear?ref=main",
                    docs + "Research/?ref=main", docs + "README.md?ref=other", docs + "Research/only-here.md?ref=main",
                    docs + "README.md?ref=feature/x", "repos/acme/nope/contents/README.md?ref=main",
                    "repos/Acme/docs/contents/README.md?ref=main", "repos/acme/widgets/contents/README.md?ref=main"):
            for argv in (("api", url), ("api", "-H", RAW, url)):
                with self.subTest(argv):
                    res = self.gh(*argv)
                    self.assertEqual((res.returncode, res.stdout, res.stderr), (1, "", "gh: Not Found (HTTP 404)\n"))

    def test_no_contents_key_is_a_404_for_every_path(self):
        self.write({"repos": {"acme/widgets": REPO}})
        res = self.gh("api", "repos/acme/docs/contents/README.md?ref=main")
        self.assertEqual((res.returncode, res.stdout, res.stderr), (1, "", "gh: Not Found (HTTP 404)\n"))

    def test_any_other_argv_is_a_usage_error(self):
        docs = "repos/acme/docs/contents"
        url = f"{docs}/README.md?ref=main"
        for argv in (("api", "-H", "Accept: application/json", url), ("api", "-H", RAW.lower(), url),
                     ("api", "-H", RAW), ("api", "-H", url), ("api", "-H", RAW, "-H", RAW, url),
                     ("api", url, "-H", RAW), ("api", "--header", RAW, url), ("api", "-H", RAW, "repos/acme/widgets"),
                     ("api", f"{docs}/README.md"), ("api", f"{docs}/README.md?ref="),
                     ("api", f"{docs}/README.md?branch=main"), ("api", f"{docs}/?ref=main"),
                     ("api", f"{docs}?ref=main"), ("api", f"{docs}/README.md?ref=a?b"),
                     ("api", url, "--jq", ".name"), ("api", url, url)):
            with self.subTest(argv):
                res = self.gh(*argv)
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("fake_gh.py:", res.stderr)
                self.assertNotIn("HTTP", res.stderr)

    def test_a_bad_contents_shape_or_another_key_is_an_error(self):
        path = {"main": {"a.md": "x"}}
        for name, scenario in {"a list": {"contents": []}, "a repo not an object": {"contents": {"acme/docs": 1}},
                               "a branch not an object": {"contents": {"acme/docs": {"main": []}}},
                               "a text not a string": {"contents": {"acme/docs": {"main": {"a.md": 5}}}},
                               "a null text": {"contents": {"acme/docs": {"main": {"a.md": None}}}},
                               "another key": {"contents": {"acme/docs": path}, "orgs": {}}}.items():
            self.write({"repos": {"acme/widgets": REPO}, **scenario})
            for argv in (("api", "repos/acme/docs/contents/a.md?ref=main"), ("api", "repos/acme/widgets")):
                with self.subTest(name, argv=argv):
                    res = self.gh(*argv)
                    self.assertEqual((res.returncode, res.stdout), (1, ""))
                    self.assertIn("fake_gh.py:", res.stderr)
                    self.assertNotIn("HTTP", res.stderr)

    def test_repos_stays_required(self):
        self.write({"contents": CONTENTS})
        res = self.gh("api", "repos/acme/docs/contents/README.md?ref=main")
        self.assertEqual((res.returncode, res.stdout), (1, ""))
        self.assertIn("fake_gh.py:", res.stderr)


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
