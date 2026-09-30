import io, json, os, re, shutil, subprocess, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import HEADER  # noqa: E402
import eng, pipeline, research  # noqa: E402
import test_eng as te  # noqa: E402

ok = te.ok
SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "89abcdef0123456789abcdef0123456789abcdef"

class FakeRun(te.FakeRun):
    """test_eng's FakeRun; a callable result is called with the argv."""
    def __call__(self, argv, timeout):
        res = super().__call__(argv, timeout)
        return res(argv) if callable(res) else res

def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)

class Prepare(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", self.root]))
        self.pg = os.path.join(self.root, "playground")
        os.makedirs(self.pg)
        self.clone = os.path.join(self.pg, "agent-pm")
        work = os.path.join(self.root, "work")
        patcher = mock.patch.object(pipeline, "WORK", work)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = os.path.join(work, "TASK-26")
        self.src = os.path.join(self.base, "src")
        self.wt = os.path.join(self.src, "agent-pm")
        self.api = ("gh", "api", "repos/ophis/agent-pm")
        self.get_url = ("git", "-C", self.clone, "remote", "get-url", "origin")
        self.get_push_url = ("git", "-C", self.clone, "remote", "get-url", "--push", "origin")
        self.fetch = ("git", "-C", self.clone, "fetch", "origin")
        self.add = ("git", "-c", "core.symlinks=false", "-C", self.clone, "worktree", "add", "--detach", self.wt, "origin/main")

    def worktree(self, path, head=SHA, clone=None):
        """What `git worktree add` leaves: path/.git and its gitdir's HEAD."""
        gitdir = os.path.join(clone or self.clone, ".git", "worktrees", os.path.basename(path))
        put(os.path.join(path, ".git"), f"gitdir: {gitdir}\n")
        put(os.path.join(gitdir, "HEAD"), head + "\n")

    def added(self, argv):
        self.worktree(argv[-2])
        return ok()

    def table(self, **over):
        t = {"api": ok('{"default_branch": "main", "permissions": {"push": false}}'), "clone": ok(),
             "fetch_url": ok("git@github.com:ophis/agent-pm.git\n"), "push_url": ok("git@github.com:ophis/agent-pm.git\n"),
             "fetch": ok(), "add": self.added}
        t.update(over)
        return [(self.api, t["api"]), (("gh", "repo", "clone"), t["clone"]), (self.get_push_url, t["push_url"]),
                (self.get_url, t["fetch_url"]), (self.fetch, t["fetch"]), (self.add, t["add"])]

    def prepare(self, desc="Repo: ophis/agent-pm", repos=None, project=None, **over):
        self.run_ = FakeRun(self.table(**over))
        return research.prepare("TASK-26", te.gql_for(desc, project=project), self.run_, repos or {}, playground=self.pg)

    def cli(self, desc="Repo: ophis/agent-pm", env=None, gql=None, tail="", project=None, **over):
        self.run_ = FakeRun(self.table(**over))
        config = os.path.join(self.root, "pipeline.toml")
        put(config, HEADER + tail)
        out, err = io.StringIO(), io.StringIO()
        rc = research.main(["prepare"], env={"AGENT_PM_ISSUE": "TASK-26"} if env is None else env,
                           gql=gql or te.gql_for(desc, project=project), run=self.run_, out=out, err=err, config=config, playground=self.pg)
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def argvs(self):
        return [c[0] for c in self.run_.calls]

    def expected(self, **over):
        return {"repo": "ophis/agent-pm", "mapped": False, "clone": self.clone, "default": "main", "worktree": self.wt,
                "commit": SHA, "reused": False, **over}

    def assert_untouched(self):
        self.assertNotIn(self.fetch, self.argvs())
        self.assertNotIn(self.add, self.argvs())

    def test_new_worktree_fetches_then_adds_a_detached_worktree(self):
        os.makedirs(self.clone)
        self.assertEqual(self.prepare(), self.expected())
        self.assertEqual(self.argvs(), [self.api, self.get_url, self.get_push_url, self.fetch, self.add])
        self.assertEqual(self.run_.calls[-2:], [(self.fetch, eng.LONG), (self.add, eng.LONG)])

    def test_missing_clone_is_cloned_then_fetched(self):
        self.assertEqual(self.prepare(), self.expected())
        self.assertEqual(self.argvs(), [self.api, ("gh", "repo", "clone", "ophis/agent-pm", self.clone), self.fetch, self.add])

    def test_commit_is_the_new_worktrees_head(self):
        os.makedirs(self.clone)
        def added(argv):
            self.worktree(argv[-2], head=OTHER)
            return ok()
        self.assertEqual(self.prepare(add=added), self.expected(commit=OTHER))

    def test_cli_prints_one_json_line(self):
        os.makedirs(self.clone)
        self.assertEqual(self.cli(), 0, self.err)
        self.assertEqual((self.out.count("\n"), self.out[-1:], self.err), (1, "\n", ""))
        self.assertEqual(json.loads(self.out), self.expected())

    def test_mapping_or_repo_line(self):
        os.makedirs(self.clone)
        project = {"id": te.PROJ}
        self.assertEqual(self.prepare("no repo here", repos={te.PROJ: "ophis/agent-pm"}, project=project), self.expected(mapped=True))
        shutil.rmtree(self.src)
        self.assertEqual(self.prepare("Repo: ophis/agent-pm", repos={te.PROJ: "ophis/other"}, project=project), self.expected())
        shutil.rmtree(self.src)
        self.assertEqual(self.cli("no repo here", tail=f'[project_repos]\n"{te.PROJ}" = "ophis/agent-pm"\n', project=project), 0, self.err)
        self.assertEqual(json.loads(self.out), self.expected(mapped=True))

    def test_no_push_permission_needed(self):
        os.makedirs(self.clone)
        self.assertEqual(self.prepare(api=ok('{"default_branch": "main", "permissions": {"push": false}}')), self.expected())

    def test_reuse_runs_no_fetch_no_add_and_no_git_in_the_tree(self):
        os.makedirs(self.clone)
        self.worktree(self.wt, head=OTHER)
        self.assertEqual(self.prepare(), self.expected(commit=OTHER, reused=True))
        self.assertEqual(self.argvs(), [self.api, self.get_url, self.get_push_url])

    def test_reuse_refusals_exit_2(self):
        os.makedirs(self.clone)
        def linked_worktree():
            real = os.path.join(self.src, "real")
            self.worktree(real)
            os.symlink(real, self.wt)
        def linked_outside():
            out = os.path.join(self.root, "agent-pm")
            self.worktree(out)
            os.makedirs(self.src)
            os.symlink(out, self.wt)
        def no_head():
            self.worktree(self.wt)
            os.remove(os.path.join(self.clone, ".git", "worktrees", "agent-pm", "HEAD"))
        cases = {"symlink to a worktree in src": linked_worktree, "symlink out of src": linked_outside,
                 "dangling symlink": lambda: (os.makedirs(self.src), os.symlink(os.path.join(self.root, "nowhere"), self.wt)),
                 "another clone's worktree": lambda: self.worktree(self.wt, clone=os.path.join(self.pg, "other")),
                 "no .git": lambda: os.makedirs(self.wt), ".git directory": lambda: os.makedirs(os.path.join(self.wt, ".git")),
                 "a file": lambda: put(self.wt, SHA), "branch HEAD": lambda: self.worktree(self.wt, head="ref: refs/heads/main"),
                 "missing HEAD": no_head, "short HEAD": lambda: self.worktree(self.wt, head=SHA[:12]),
                 "not a hex HEAD": lambda: self.worktree(self.wt, head="z" * 40)}
        for name, make in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.base, ignore_errors=True)
                make()
                self.assertEqual(self.cli(), 2)
                self.assertRegex(self.err, rf"\Aresearch\.py: {re.escape(self.wt)}\b[^\n]*\n\Z")
                self.assertEqual(self.out, "")
                self.assert_untouched()

    def test_every_invalid_target_exits_2(self):
        os.makedirs(self.clone)
        cases = [({"desc": "no repo here"}, te.NO_LINE.reason),
                 ({"desc": "Repo: nope"}, "unreadable Repo line: 'nope'"),
                 ({"desc": "Repo: ophis/a\nRepo: ophis/b"}, "several different Repo: values"),
                 ({"gql": lambda q, **v: {"issue": None}}, "TASK-26: issue not found"),
                 ({"gql": te.gql_for("Repo: ophis/agent-pm", ident="TASK-27")}, "Linear returned 'TASK-27' for TASK-26"),
                 ({"api": ok(code=1, stderr="gh: Not Found (HTTP 404)")}, "ophis/agent-pm: not found or no access (HTTP 404)"),
                 ({"api": ok(code=1, stderr="gh: Forbidden (HTTP 403)")}, "ophis/agent-pm: not found or no access (HTTP 403)"),
                 ({"api": ok('{"default_branch": "ma$(x)", "permissions": {"push": false}}')}, "ophis/agent-pm: unsafe default branch name"),
                 ({"fetch_url": ok("git@github.com:other/agent-pm.git\n")},
                  f"{self.clone} is not a clone of ophis/agent-pm (origin URL differs)"),
                 ({"push_url": ok("https://evil.example/x.git\n")},
                  f"{self.clone} is not a clone of ophis/agent-pm (origin push URL differs)")]
        for kwargs, reason in cases:
            with self.subTest(reason):
                self.assertEqual(self.cli(**kwargs), 2)
                self.assertEqual((self.out, self.err), ("", f"research.py: {reason}\n"))
                self.assert_untouched()
        self.assertEqual(self.cli("no repo here", tail=f'[project_repos]\n"{te.PROJ}" = "ophis/agent-pm"\n', project={"id": te.PROJ},
                                  api=ok(code=1, stderr="gh: Not Found (HTTP 404)")), 2)
        self.assertEqual(self.err, f"research.py: {eng.MAPPED}ophis/agent-pm: not found or no access (HTTP 404)\n")

    def test_clone_symlink_exits_2(self):
        other = os.path.join(self.root, "elsewhere")
        os.makedirs(other)
        os.symlink(other, self.clone)
        self.assertEqual(self.cli(), 2)
        self.assertEqual(self.err, f"research.py: {self.clone} is a symlink or outside {self.pg}\n")
        self.assert_untouched()

    def test_src_symlink_escaping_the_run_dir_exits_2(self):
        os.makedirs(self.clone)
        outside = os.path.join(self.root, "outside")
        os.makedirs(outside)
        os.makedirs(self.base)
        os.symlink(outside, self.src)
        self.assertEqual(self.cli(), 2)
        self.assertEqual(self.err, f"research.py: {self.src} resolves outside {self.base}\n")
        self.assert_untouched()
        self.assertEqual(os.listdir(outside), [])

    def test_transient_or_git_failure_exits_1(self):
        def linear_down(q, **v):
            raise SystemExit("linear api error: boom")
        cases = [({"gql": linear_down}, "Linear: linear api error: boom"),
                 ({"api": ok(code=1, stderr="gh: Bad Gateway (HTTP 502)")}, None),
                 ({"api": ok("not json")}, None),
                 ({"fetch": ok(code=1, stderr="fatal: unable to access")}, None),
                 ({"fetch": subprocess.TimeoutExpired("git", 600)}, None),
                 ({"add": ok(code=128, stderr="fatal: invalid reference: origin/main")}, None),
                 ({"add": subprocess.TimeoutExpired("git", 600)}, None)]
        os.makedirs(self.clone)
        for kwargs, reason in cases:
            with self.subTest(kwargs):
                shutil.rmtree(self.base, ignore_errors=True)
                self.assertEqual(self.cli(**kwargs), 1)
                self.assertEqual(self.out, "")
                self.assertRegex(self.err, r"\Aresearch\.py: [^\n]+\n\Z")
                if reason:
                    self.assertEqual(self.err, f"research.py: {reason}\n")
        self.assertEqual(self.cli(fetch=ok(code=1, stderr="network")), 1)
        self.assertNotIn(self.add, self.argvs())
        shutil.rmtree(self.clone)
        self.assertEqual(self.cli(clone=ok(code=1, stderr="network")), 1)
        self.assert_untouched()

    def test_bad_or_missing_issue_env_exits_1(self):
        def no_linear(q, **v):
            raise AssertionError("Linear read")
        for env in ({}, {"AGENT_PM_ISSUE": ""}, {"AGENT_PM_ISSUE": "task-26"}, {"AGENT_PM_ISSUE": "TASK-26\n"},
                    {"AGENT_PM_ISSUE": "../TASK-26"}, {"AGENT_PM_ISSUE": "TASK-26/src"}):
            with self.subTest(env):
                self.assertEqual(self.cli(env=env, gql=no_linear), 1)
                self.assertEqual((self.out, self.err), ("", "research.py: AGENT_PM_ISSUE is not set to an issue id\n"))
                self.assertEqual(self.run_.calls, [])

    def test_broken_config_exits_1(self):
        self.assertEqual(self.cli(tail=f'[project_repos]\n"{te.PROJ}" = "ophis"\n'), 1)
        self.assertTrue(self.err.startswith(f"research.py: pipeline.toml: project_repos.{te.PROJ} "), self.err)
        self.assertEqual((self.out, self.run_.calls), ("", []))

    def test_only_reads_fetch_and_worktree_add_touch_the_clone(self):
        allowed = {self.api, ("gh", "repo", "clone", "ophis/agent-pm", self.clone), self.get_url, self.get_push_url, self.fetch, self.add}
        seen = []
        self.prepare()
        seen += self.argvs()
        shutil.rmtree(self.src)
        self.prepare()
        seen += self.argvs()
        self.prepare()
        seen += self.argvs()
        self.assertLessEqual(set(seen), allowed)
        self.assertIn(("gh", "repo", "clone", "ophis/agent-pm", self.clone), seen)
        self.assertEqual(seen.count(self.add), 2)
        self.assertFalse([a for a in seen if a != self.add and any(self.src in str(x) for x in a)])

if __name__ == "__main__":
    unittest.main()
