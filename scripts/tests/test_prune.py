import io, os, sys, tempfile, unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import prune  # noqa: E402


def ok(stdout="", code=0, stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


class FakeRun:
    """Maps argv prefixes (tuples) to results; records calls."""
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        for prefix, res in self.table:
            if tuple(argv[:len(prefix)]) == prefix:
                if isinstance(res, BaseException):
                    raise res
                return res
        raise AssertionError(f"unexpected {argv}")


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
DONE, CANCELED, PROG = "state-done", "state-canceled", "state-progress"


def iso(dt):
    return dt.isoformat()


def gql_for(issues, details):
    """issues: [(id, identifier)]; details: {id: (state_id, [(createdAt, toStateId)], createdAt)}."""
    names = {DONE: "Done", CANCELED: "Canceled", PROG: "In Progress"}

    def gql(query, **v):
        if "workflowStates" in query:
            return {"teams": {"nodes": [{"id": "team"}]},
                    "workflowStates": {"nodes": [{"id": DONE, "name": "Done"},
                                                {"id": CANCELED, "name": "Canceled"},
                                                {"id": PROG, "name": "In Progress"}]}}
        if "history" in query:
            sid, hist, created = details[v["i"]]
            return {"issue": {"identifier": v["i"], "createdAt": created,
                              "state": {"name": names[sid]},
                              "history": {"nodes": [{"createdAt": c, "toStateId": s}
                                                    for c, s in hist]}}}
        return {"issues": {"nodes": [{"id": i, "identifier": n} for i, n in issues]}}
    return gql


def git_table(wt, branch="TASK-49-x", dirty="", unpushed="0", upstream=True,
              registered=True, head="ref"):
    wt_real = os.path.realpath(wt)
    table = [
        (("git", "-C", wt, "rev-parse", "--is-inside-work-tree"), ok("true\n")),
        (("git", "-C", wt, "worktree", "list", "--porcelain"),
         ok(f"worktree {wt_real}\nHEAD abc\nbranch refs/heads/{branch}\n\n"
            f"worktree /pg/repo\nHEAD def\nbranch refs/heads/main\n" if registered
            else "worktree /pg/repo\nHEAD def\nbranch refs/heads/main\n")),
        (("git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"),
         ok("HEAD\n" if head == "detached" else f"{branch}\n")),
        (("git", "-C", wt, "status", "--porcelain"), ok(dirty)),
        (("git", "-C", wt, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"),
         ok(f"origin/{branch}\n") if upstream else ok("", code=128, stderr="no upstream")),
        (("git", "-C", wt, "rev-list", "--count", "@{u}..HEAD"), ok(f"{unpushed}\n")),
        (("git", "-C", wt, "rev-parse", "--git-common-dir"), ok("/pg/repo/.git\n")),
        (("git", "-C", "/pg/repo", "worktree", "remove"), ok("")),
        (("git", "-C", "/pg/repo", "branch", "-d"), ok("")),
    ]
    return table


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(lambda: os.system(f"rm -rf {self.work}"))
        self.cfg = {"team": "Frank's Agents"}

    def mkw(self, ident, branch):
        d = os.path.join(self.work, ident, "worktrees", branch)
        os.makedirs(d)
        return d

    def pruner(self, gql, run, dry=False):
        p = prune.Pruner(gql, self.cfg, NOW, dry, run=run, work=self.work)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = p.run()
        return code, buf.getvalue()

    def hist(self, *moves):
        return [(iso(NOW - timedelta(hours=h)), s) for h, s in moves]

    def test_done_old_cleaned(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(git_table(wt))
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (DONE, self.hist((30, DONE)), iso(NOW - timedelta(hours=40)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-49/TASK-49-x", out)
        argv = [c[0] for c in run.calls]
        self.assertIn(("git", "-C", "/pg/repo", "worktree", "remove", os.path.realpath(wt)), argv)
        self.assertIn(("git", "-C", "/pg/repo", "branch", "-d", "TASK-49-x"), argv)

    def test_canceled_old_cleaned(self):
        wt = self.mkw("TASK-37", "TASK-37-x")
        run = FakeRun(git_table(wt, branch="TASK-37-x"))
        gql = gql_for([("TASK-37", "TASK-37")],
                      {"TASK-37": (CANCELED, self.hist((50, CANCELED)), iso(NOW - timedelta(hours=60)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-37/TASK-37-x", out)

    def test_done_young_untouched(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(git_table(wt))
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (DONE, self.hist((1, DONE)), iso(NOW - timedelta(hours=40)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls, [])
        self.assertIn("nothing to do", out)

    def test_reopened_clock_restarts(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(git_table(wt))
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (DONE, self.hist((30, DONE), (5, PROG), (1, DONE)),
                                   iso(NOW - timedelta(hours=40)))})
        code, _ = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls, [])

    def test_dirty_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(git_table(wt, dirty=" M foo.py\n"))
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (DONE, self.hist((30, DONE)), iso(NOW - timedelta(hours=40)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/TASK-49-x: uncommitted changes", out)
        argv = [c[0][:5] for c in run.calls]
        self.assertNotIn(("git", "-C", "/pg/repo", "worktree", "remove"), argv)

    def test_unpushed_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(git_table(wt, unpushed="2"))
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (DONE, self.hist((30, DONE)), iso(NOW - timedelta(hours=40)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/TASK-49-x: unpushed commits", out)

    def test_no_upstream_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(git_table(wt, upstream=False))
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (DONE, self.hist((30, DONE)), iso(NOW - timedelta(hours=40)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("no upstream", out)

    def test_detached_head_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(git_table(wt, head="detached"))
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (DONE, self.hist((30, DONE)), iso(NOW - timedelta(hours=40)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("detached HEAD", out)

    def test_unregistered_worktree_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(git_table(wt, registered=False))
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (DONE, self.hist((30, DONE)), iso(NOW - timedelta(hours=40)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("not a registered worktree", out)

    def test_dry_run_changes_nothing(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(git_table(wt))
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (DONE, self.hist((30, DONE)), iso(NOW - timedelta(hours=40)))})
        code, out = self.pruner(gql, run, dry=True)
        self.assertEqual(code, 0)
        self.assertIn("prune-plan TASK-49/TASK-49-x", out)
        argv = [c[0][:5] for c in run.calls]
        self.assertNotIn(("git", "-C", "/pg/repo", "worktree", "remove"), argv)
        self.assertNotIn(("git", "-C", "/pg/repo", "branch", "-d"), argv)

    def test_transient_git_error_reported(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        table = git_table(wt)
        table[3] = (("git", "-C", wt, "status", "--porcelain"), OSError("boom"))
        run = FakeRun(table)
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (DONE, self.hist((30, DONE)), iso(NOW - timedelta(hours=40)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 3)
        self.assertIn("prune-error TASK-49/TASK-49-x", out)

    def test_no_worktrees_dir(self):
        run = FakeRun([])
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (DONE, self.hist((30, DONE)), iso(NOW - timedelta(hours=40)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls, [])
        self.assertIn("nothing to do", out)

    def test_no_worktrees_no_linear_queries(self):
        run = FakeRun([])
        inner = gql_for([], {})
        calls = []

        def gql(query, **v):
            calls.append(query)
            return inner(query, **v)

        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertFalse([q for q in calls if "history" in q])  # only the setup query runs

    def test_unfinished_issue_untouched(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(git_table(wt))
        gql = gql_for([("TASK-49", "TASK-49")],
                      {"TASK-49": (PROG, self.hist((30, PROG)), iso(NOW - timedelta(hours=40)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls, [])
        self.assertIn("nothing to do", out)


if __name__ == "__main__":
    unittest.main()
