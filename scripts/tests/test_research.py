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

def sequence(*results):
    """A FakeRun result giving each call the next of results."""
    it = iter(results)
    return lambda argv: next(it)

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
        patcher = mock.patch("time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        self.base = os.path.join(work, "TASK-26")
        self.src = os.path.join(self.base, "src")
        self.wt = os.path.join(self.src, "agent-pm")
        self.api = ("gh", "api", "repos/ophis/agent-pm")
        self.get_url = ("git", "-C", self.clone, "remote", "get-url", "origin")
        self.get_push_url = ("git", "-C", self.clone, "remote", "get-url", "--push", "origin")
        self.fetch = ("git", "-C", self.clone, "fetch", "origin")
        self.add = ("git", "-c", "core.symlinks=false", "-C", self.clone, "worktree", "add", "--detach", self.wt, "origin/main")
        self.remove = ("git", "-C", self.clone, "worktree", "remove", "--force", "--force", "--", self.wt)
        self.gitdir = os.path.join(self.clone, ".git", "worktrees", "agent-pm")

    def worktree(self, path, head=SHA, clone=None):
        """What `git worktree add` leaves: path/.git and its gitdir's HEAD."""
        gitdir = os.path.join(clone or self.clone, ".git", "worktrees", os.path.basename(path))
        put(os.path.join(path, ".git"), f"gitdir: {gitdir}\n")
        put(os.path.join(gitdir, "HEAD"), head + "\n")

    def added(self, argv):
        self.worktree(argv[-2])
        return ok()

    def removed(self, argv):
        shutil.rmtree(argv[-1])
        shutil.rmtree(self.gitdir)
        return ok()

    def table(self, **over):
        t = {"api": ok('{"default_branch": "main", "permissions": {"push": false}}'), "clone": ok(),
             "fetch_url": ok("git@github.com:ophis/agent-pm.git\n"), "push_url": ok("git@github.com:ophis/agent-pm.git\n"),
             "fetch": ok(), "add": self.added, "remove": self.removed}
        t.update(over)
        return [(self.api, t["api"]), (("gh", "repo", "clone"), t["clone"]), (self.get_push_url, t["push_url"]),
                (self.get_url, t["fetch_url"]), (self.fetch, t["fetch"]), (self.add, t["add"]), (self.remove, t["remove"])]

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
        self.assertNotIn(self.remove, self.argvs())
        self.assertNotIn(self.fetch, self.argvs())
        self.assertNotIn(self.add, self.argvs())

    def test_new_worktree_fetches_then_adds_a_detached_worktree(self):
        os.makedirs(self.clone)
        self.assertEqual(self.prepare(), self.expected())
        self.assertEqual(self.argvs(), [self.api, self.get_url, self.get_push_url, self.fetch, self.add])
        self.assertEqual(self.run_.calls[-2:], [(self.fetch, eng.LONG), (self.add, eng.LONG)])

    def test_a_failing_fetch_is_retried_up_to_3_times_then_adds(self):
        os.makedirs(self.clone)
        self.assertEqual(self.prepare(fetch=sequence(ok(code=1, stderr="lock"), ok(code=1, stderr="lock"), ok())), self.expected())
        self.assertEqual(self.argvs().count(self.fetch), 3)
        self.assertEqual(self.argvs()[-1], self.add)
        self.assertEqual(self.sleep.call_args_list, [mock.call(3)] * 2)

    def test_a_fetch_failing_4_times_exits_1_with_the_last_stderr(self):
        os.makedirs(self.clone)
        self.assertEqual(self.cli(fetch=sequence(*[ok(code=1, stderr=f"lock {i}") for i in range(1, 5)])), 1)
        self.assertEqual((self.out, self.err), ("", "research.py: git fetch: lock 4\n"))
        self.assertEqual(self.argvs().count(self.fetch), 4)
        self.assertNotIn(self.add, self.argvs())
        self.assertEqual(self.sleep.call_args_list, [mock.call(3)] * 3)

    def test_a_failing_worktree_add_is_not_retried(self):
        os.makedirs(self.clone)
        self.assertEqual(self.cli(add=ok(code=128, stderr="fatal: busy")), 1)
        self.assertEqual((self.out, self.err), ("", "research.py: git worktree add: fatal: busy\n"))
        self.assertEqual((self.argvs().count(self.fetch), self.argvs().count(self.add)), (1, 1))
        self.sleep.assert_not_called()

    def test_cli_prints_one_json_line(self):
        os.makedirs(self.clone)
        mapping = {"desc": "no repo here", "tail": f'[project_repos]\n"{te.PROJ}" = "ophis/agent-pm"\n', "project": {"id": te.PROJ}}
        for name, kwargs, mapped in (("Repo line", {}, False), ("project mapping", mapping, True)):
            with self.subTest(name):
                shutil.rmtree(self.src, ignore_errors=True)
                self.assertEqual(self.cli(**kwargs), 0, self.err)
                self.assertEqual((self.out.count("\n"), self.out[-1:], self.err), (1, "\n", ""))
                self.assertEqual(json.loads(self.out), self.expected(mapped=mapped))

    def test_reuse_runs_no_fetch_no_add_and_no_git_in_the_tree(self):
        os.makedirs(self.clone)
        self.worktree(self.wt, head=OTHER)
        self.assertEqual(self.prepare(), self.expected(commit=OTHER, reused=True))
        self.assertEqual(self.argvs(), [self.api, self.get_url, self.get_push_url])

    def test_an_interrupted_add_is_removed_then_added_again(self):
        os.makedirs(self.clone)
        for name, head in (("locked with a HEAD", OTHER), ("locked before HEAD", None)):
            with self.subTest(name):
                shutil.rmtree(self.base, ignore_errors=True)
                self.worktree(self.wt, head=OTHER)
                put(os.path.join(self.wt, "partial"), "")
                put(os.path.join(self.gitdir, "locked"), "initializing\n")
                if head is None:
                    os.remove(os.path.join(self.gitdir, "HEAD"))
                self.assertEqual(self.prepare(), self.expected())
                self.assertEqual(self.argvs(), [self.api, self.get_url, self.get_push_url, self.remove, self.fetch, self.add])
                self.assertEqual(self.run_.calls[3], (self.remove, eng.LONG))
                self.assertFalse(os.path.exists(os.path.join(self.wt, "partial")))

    def test_a_failed_remove_exits_1(self):
        os.makedirs(self.clone)
        self.worktree(self.wt)
        put(os.path.join(self.gitdir, "locked"), "initializing\n")
        self.assertEqual(self.cli(remove=ok(code=1, stderr="fatal: busy")), 1)
        self.assertEqual((self.out, self.err), ("", "research.py: git worktree remove: fatal: busy\n"))
        self.assertEqual(self.argvs()[-1], self.remove)
        self.assertNotIn(self.fetch, self.argvs())

    def test_src_symlink_exits_2(self):
        os.makedirs(self.clone)
        inside, outside = os.path.join(self.base, "worktrees"), os.path.join(self.root, "outside")
        moved = f"{self.wt} resolves to {os.path.join(os.path.realpath(inside), 'agent-pm')}"
        for name, real, existing, reason in (("inside the run dir, reuse", inside, True, moved),
                                             ("inside the run dir, new", inside, False, moved),
                                             ("escaping the run dir", outside, False, f"{self.src} resolves outside {self.base}")):
            with self.subTest(name):
                shutil.rmtree(self.base, ignore_errors=True)
                os.makedirs(self.base)
                os.makedirs(real)
                os.symlink(real, self.src)
                if existing:
                    self.worktree(os.path.join(real, "agent-pm"))
                self.assertEqual(self.cli(), 2)
                self.assertEqual((self.out, self.err), ("", f"research.py: {reason}\n"))
                self.assert_untouched()
                self.assertEqual(os.listdir(real), ["agent-pm"] if existing else [])

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
                 "another clone's locked worktree": lambda: (self.worktree(self.wt, clone=os.path.join(self.pg, "other")),
                                                            put(os.path.join(self.pg, "other", ".git", "worktrees", "agent-pm", "locked"), "")),
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

    def test_invalid_issue_or_target_exits_2(self):
        other = os.path.join(self.root, "elsewhere")
        os.makedirs(other)
        os.symlink(other, self.clone)
        for name, kwargs, reason in (("another issue", {"gql": te.gql_for("Repo: ophis/agent-pm", ident="TASK-27")}, "Linear returned 'TASK-27' for TASK-26"),
                                     ("clone symlink", {}, f"{self.clone} is a symlink or outside {self.pg}")):
            with self.subTest(name):
                self.assertEqual(self.cli(**kwargs), 2)
                self.assertEqual((self.out, self.err), ("", f"research.py: {reason}\n"))
                self.assert_untouched()

    def test_transient_or_git_failure_exits_1(self):
        def linear_down(q, **v):
            raise SystemExit("linear api error: boom")
        timeout = subprocess.TimeoutExpired("git", 600)
        cases = [({"gql": linear_down}, "Linear: linear api error: boom"),
                 ({"api": ok(code=1, stderr="gh: Bad Gateway (HTTP 502)")}, "gh api repos/ophis/agent-pm: gh: Bad Gateway (HTTP 502)"),
                 ({"api": ok("not json")}, "prepare: JSONDecodeError('Expecting value: line 1 column 1 (char 0)')"),
                 ({"api": ok('{"message": "Bad credentials"}')}, "prepare: KeyError('permissions')"),
                 ({"api": ok("[]")}, "prepare: TypeError('list indices must be integers or slices, not str')"),
                 ({"fetch": ok(code=1, stderr="fatal: unable to access")}, "git fetch: fatal: unable to access"),
                 ({"fetch": timeout}, f"prepare: {timeout!r}"),
                 ({"add": ok(code=128, stderr="fatal: invalid reference: origin/main")}, "git worktree add: fatal: invalid reference: origin/main"),
                 ({"add": timeout}, f"prepare: {timeout!r}"),
                 ({"fetch": FileNotFoundError(2, "No such file or directory", "git")},
                  "prepare: FileNotFoundError(2, 'No such file or directory')")]
        os.makedirs(self.clone)
        for kwargs, reason in cases:
            with self.subTest(reason):
                shutil.rmtree(self.base, ignore_errors=True)
                self.assertEqual(self.cli(**kwargs), 1)
                self.assertEqual((self.out, self.err), ("", f"research.py: {reason}\n"))
        self.assertEqual(self.cli(fetch=ok(code=1, stderr="network")), 1)
        self.assertNotIn(self.add, self.argvs())
        shutil.rmtree(self.clone)
        self.assertEqual(self.cli(clone=ok(code=1, stderr="network")), 1)
        self.assertEqual(self.err, "research.py: gh repo clone ophis/agent-pm: network\n")
        self.assert_untouched()

    def test_an_unexpected_command_is_not_an_exit_code(self):
        os.makedirs(self.clone)
        config = os.path.join(self.root, "pipeline.toml")
        put(config, HEADER)
        with self.assertRaisesRegex(AssertionError, "unexpected"):
            research.main(["prepare"], env={"AGENT_PM_ISSUE": "TASK-26"}, gql=te.gql_for("Repo: ophis/agent-pm"), run=FakeRun([]),
                          out=io.StringIO(), err=io.StringIO(), config=config, playground=self.pg)

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
        put(os.path.join(self.gitdir, "locked"), "initializing\n")
        self.prepare()
        self.assertEqual(self.argvs(), [self.api, self.get_url, self.get_push_url, self.remove, self.fetch, self.add])

if __name__ == "__main__":
    unittest.main()
