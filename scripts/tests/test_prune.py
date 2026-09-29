import io, os, re, shutil, sys, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import prune  # noqa: E402


def ok(stdout="", code=0, stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def g(cwd, *args):
    """argv of a git call as prune makes it."""
    return ("git", *prune.SAFE, "-C", cwd, *args)


class FakeRun:
    """Maps argv prefixes (tuples) to results; records calls. A result may be an
    exception to raise, or a function of the calls so far (to model git state)."""
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, argv, timeout):
        self.calls.append(tuple(argv))
        for prefix, res in self.table.items():
            if tuple(argv[:len(prefix)]) == prefix:
                if callable(res):
                    res = res(self.calls)
                if isinstance(res, BaseException):
                    raise res
                return res
        raise AssertionError(f"unexpected {argv}")

    def ran(self, *args):
        """Calls whose git arguments (after -C <dir>) start with args."""
        n = len(g(""))
        return [c for c in self.calls if c[n:n + len(args)] == args]


NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
STATE_IDS = {"Done": "state-done", "Canceled": "state-canceled", "In Progress": "state-progress"}
DONE, CANCELED, PROG = STATE_IDS.values()
SHA = "def456"


def iso(dt):
    return dt.isoformat()


def hist(*moves):
    """[(age, toStateId)], age in hours or a timedelta."""
    return [(iso(NOW - (h if isinstance(h, timedelta) else timedelta(hours=h))), s) for h, s in moves]


def gql_for(details, states=tuple(STATE_IDS)):
    """details: {identifier: (state_id or an exception to raise, history)}."""
    names = {v: k for k, v in STATE_IDS.items()}

    def gql(query, **v):
        gql.calls.append(query)
        if "workflowStates" in query:
            return {"workflowStates": {"nodes": [{"id": STATE_IDS[s], "name": s} for s in states]}}
        sid, moves = details[v["i"]]
        if isinstance(sid, BaseException):
            raise sid
        return {"issue": {"identifier": v["i"], "createdAt": iso(NOW - timedelta(hours=400)),
                          "state": {"name": names[sid]},
                          "history": {"nodes": [{"createdAt": c, "toStateId": s} for c, s in moves]}}}
    gql.calls = []
    return gql


def override(table, word, res):
    """Replace the result of the one call whose argv contains word."""
    (key,) = [k for k in table if word in k]
    table[key] = res


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class PruneTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        self.work = os.path.join(root, "work")
        self.clone = os.path.realpath(os.path.join(root, "clone"))
        os.makedirs(self.work)
        self.cfg = {"team": "Frank's Agents"}
        self.listed = []  # the clone's worktree list: (real path, branch or None if detached)

    def mkw(self, ident, name, branch=None, detached=False, register=True, clone=None):
        """A linked worktree as git lays it out on disk (files only, no repository)."""
        clone = clone or self.clone
        wt = os.path.join(self.work, ident, "worktrees", name)
        gitdir = os.path.join(clone, ".git", "worktrees", name)
        os.makedirs(wt)
        os.makedirs(gitdir)
        write(os.path.join(wt, ".git"), f"gitdir: {gitdir}\n")
        write(os.path.join(gitdir, "gitdir"), os.path.join(os.path.realpath(wt), ".git") + "\n")
        if register:
            self.listed.append((os.path.realpath(wt), None if detached else branch or name))
        return wt

    def listing(self):
        recs = [f"worktree {self.clone}\0HEAD 0\0branch refs/heads/main\0"]
        recs += [f"worktree {p}\0HEAD 1\0" + (f"branch refs/heads/{b}\0" if b else "detached\0")
                 for p, b in self.listed]
        return ok("".join(r + "\0" for r in recs))

    def table(self, wt, branch=None, status="", upstream=None, remote="ok"):
        """remote: ok | diverged (lacks our commits) | missing (no such branch) |
        down (unreachable) | unfetched (ahead, with commits not yet in the clone)."""
        path = os.path.realpath(wt)
        branch = branch or os.path.basename(wt)
        upstream = f"refs/remotes/origin/{branch}" if upstream is None else upstream
        m = re.fullmatch(r"refs/remotes/([^/]+)/(.+)", upstream)
        rname, rbranch = m.groups() if m else ("?", "?")
        fetch = g(self.clone, "fetch", "--", rname, f"+refs/heads/{rbranch}:{upstream}")
        listed = ok(f"{SHA}\trefs/heads/{rbranch}\n")
        ls, at_tip, at_ref = {
            "ok": (listed, ok(), ok()),
            "diverged": (listed, ok(code=1), ok(code=1)),
            "missing": (ok(""), ok(), ok()),
            "down": (OSError("net down"), ok(), ok()),
            "unfetched": (listed, ok(code=128, stderr=f"fatal: Not a valid commit name {SHA}"), ok()),
        }[remote]
        return {
            g(self.clone, "worktree", "list", "--porcelain", "-z"): lambda calls: self.listing(),
            g(path, "status", "--porcelain", "-z", "--ignored", "--untracked-files=all"): ok(status),
            g(path, "rev-parse", "--symbolic-full-name", "@{u}"):
                ok(upstream + "\n") if upstream else ok(code=128, stderr="fatal: no upstream configured"),
            g(path, "ls-remote", "--", rname, f"refs/heads/{rbranch}"): ls,
            fetch: ok(),
            g(path, "merge-base", "--is-ancestor", "HEAD", SHA): at_tip,
            # The tracking ref is stale until fetched, and `branch -d` judges against it.
            g(path, "merge-base", "--is-ancestor", "HEAD", upstream): lambda calls: at_ref if fetch in calls else ok(code=1),
            g(self.clone, "worktree", "remove", path): ok(),
            g(self.clone, "branch", "-d", branch): lambda calls: ok() if fetch in calls else
                ok(code=1, stderr=f"error: the branch '{branch}' is not fully merged"),
        }

    def finished(self, *idents, age=30, state=DONE):
        return gql_for({i: (state, hist((age, state))) for i in idents})

    def pruner(self, gql, run, dry=False):
        p = prune.Pruner(gql, self.cfg, NOW, dry, run=run, work=self.work)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = p.run()
        return code, buf.getvalue()

    def main(self, *argv, gql, run):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = prune.main(list(argv), gql=gql, run=run, work=self.work, now=NOW)
        return code, out.getvalue(), err.getvalue()

    def assert_untouched(self, run):
        self.assertFalse(run.ran("fetch"))
        self.assertFalse(run.ran("worktree", "remove"))
        self.assertFalse(run.ran("branch", "-d"))

    def test_done_old_cleaned(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-49/TASK-49-x", out)
        self.assertIn(g(self.clone, "worktree", "remove", os.path.realpath(wt)), run.calls)
        self.assertIn(g(self.clone, "branch", "-d", "TASK-49-x"), run.calls)
        self.assertTrue(all(c[:5] == ("git", *prune.SAFE) for c in run.calls))

    def test_fetch_before_ancestry_check_and_remove(self):
        # The fake's tracking ref is stale until fetched: without the fetch first,
        # the ancestry check skips and `branch -d` refuses.
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-49/TASK-49-x", out)
        order = [run.calls.index(run.ran(*a)[0]) for a in
                 (("fetch", "--", "origin", "+refs/heads/TASK-49-x:refs/remotes/origin/TASK-49-x"),
                  ("merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/TASK-49-x"),
                  ("worktree", "remove"), ("branch", "-d"))]
        self.assertEqual(order, sorted(order))

    def test_fetch_failure_aborts_before_remove(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        table = self.table(wt)
        override(table, "fetch", OSError("net down"))
        run = FakeRun(table)
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 3)
        self.assertIn("prune-error TASK-49/TASK-49-x", out)
        self.assertFalse(run.ran("worktree", "remove"))
        self.assertFalse(run.ran("branch", "-d"))

    def test_remote_ahead_is_fetched_then_cleaned(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, remote="unfetched"))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-49/TASK-49-x", out)

    def test_remote_ahead_dry_run_skips_without_fetch(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, remote="unfetched"))
        code, out = self.pruner(self.finished("TASK-49"), run, dry=True)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/TASK-49-x: cannot compare with the remote tip", out)
        self.assert_untouched(run)

    def test_canceled_old_cleaned(self):
        wt = self.mkw("TASK-37", "TASK-37-x")
        run = FakeRun(self.table(wt))
        code, out = self.pruner(self.finished("TASK-37", age=50, state=CANCELED), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-37/TASK-37-x", out)

    def test_done_young_untouched(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        code, out = self.pruner(self.finished("TASK-49", age=1), run)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls, [])
        self.assertEqual(out, "")

    def test_quarantine_boundary(self):
        at = self.mkw("TASK-48", "TASK-48-x")
        before = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun({**self.table(at), **self.table(before)})
        gql = gql_for({"TASK-48": (DONE, hist((timedelta(hours=24), DONE))),
                       "TASK-49": (DONE, hist((timedelta(hours=24) - timedelta(seconds=1), DONE)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-48/TASK-48-x", out)
        self.assertNotIn("TASK-49", out)
        self.assertFalse([c for c in run.calls if os.path.realpath(before) in c])

    def test_reopened_clock_restarts(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        gql = gql_for({"TASK-49": (DONE, hist((30, DONE), (5, PROG), (1, DONE)))})
        code, _ = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls, [])

    def test_latest_of_done_and_canceled_counts(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        gql = gql_for({"TASK-49": (DONE, hist((50, CANCELED), (2, DONE)))})
        code, _ = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls, [])

    def test_dirty_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, status=" M foo.py\0"))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/TASK-49-x: uncommitted changes", out)
        self.assert_untouched(run)

    def test_untracked_skipped(self):
        # --untracked-files=all (enforced by the fake's argv) overrides status.showUntrackedFiles=no.
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, status="?? notes/todo.txt\0"))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/TASK-49-x: uncommitted changes", out)
        self.assert_untouched(run)

    def test_ignored_file_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, status="!! __pycache__/a.pyc\0!! .env\0"))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/TASK-49-x: 1 ignored file(s) would be lost, e.g. .env", out)
        self.assert_untouched(run)

    def test_regenerable_caches_do_not_block(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        caches = ["__pycache__/a.cpython-311.pyc", "src/b.pyc", ".pytest_cache/v/x", "node_modules/p/i.js",
                  ".venv/bin/python", ".DS_Store", "docs/.DS_Store"]
        run = FakeRun(self.table(wt, status="".join(f"!! {c}\0" for c in caches)))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-49/TASK-49-x", out)

    def test_unpushed_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, remote="diverged"))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/TASK-49-x: unpushed commits", out)
        self.assertFalse(run.ran("worktree", "remove"))

    def test_remote_branch_missing_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, remote="missing"))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("cannot confirm refs/remotes/origin/TASK-49-x on the remote", out)
        self.assert_untouched(run)

    def test_remote_unreachable_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, remote="down"))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("cannot reach the remote", out)
        self.assert_untouched(run)

    def test_non_origin_remote(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, upstream="refs/remotes/fork/feature/x"))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-49/TASK-49-x", out)
        self.assertTrue(run.ran("fetch", "--", "fork", "+refs/heads/feature/x:refs/remotes/fork/feature/x"))

    def test_unsafe_upstream_never_reaches_the_remote(self):
        a = self.mkw("TASK-49", "a")
        b = self.mkw("TASK-49", "b")
        c = self.mkw("TASK-49", "c")
        run = FakeRun({**self.table(a, upstream="refs/remotes/origin/--upload-pack=/tmp/evil.sh"),
                       **self.table(b, upstream="refs/remotes/-c/x"),
                       **self.table(c, upstream="refs/remotes/origin/a..b")})
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("unsafe upstream"), 3)
        self.assertFalse(run.ran("ls-remote"))
        self.assert_untouched(run)

    def test_unparseable_upstream_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, upstream="refs/heads/main"))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("cannot parse upstream refs/heads/main", out)
        self.assert_untouched(run)

    def test_unsafe_branch_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x", branch="-D")
        run = FakeRun(self.table(wt, branch="-D"))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/TASK-49-x: unsafe branch name", out)
        self.assertEqual(len(run.calls), 1)  # only the clone's worktree list

    def test_unknown_finish_time_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        code, out = self.pruner(gql_for({"TASK-49": (DONE, [])}), run)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls, [])
        self.assertIn("finish time unknown", out)

    def test_planted_repo_refused_before_any_git(self):
        # A repository of its own (e.g. `git init` with core.fsmonitor set) must not vouch for itself.
        wt = os.path.join(self.work, "TASK-49", "worktrees", "evil")
        os.makedirs(os.path.join(wt, ".git"))
        run = FakeRun({})
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/evil: not a linked worktree", out)
        self.assertEqual(run.calls, [])

    def test_gitdir_of_another_worktree_refused(self):
        self.mkw("TASK-50", "TASK-50-x")
        wt = os.path.join(self.work, "TASK-49", "worktrees", "evil")
        os.makedirs(wt)
        write(os.path.join(wt, ".git"), f"gitdir: {self.clone}/.git/worktrees/TASK-50-x\n")
        run = FakeRun({})
        gql = gql_for({"TASK-49": (DONE, hist((30, DONE))), "TASK-50": (PROG, hist((30, PROG)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/evil: the clone does not point back to this worktree", out)
        self.assertEqual(run.calls, [])

    def test_clone_inside_work_refused(self):
        self.mkw("TASK-49", "TASK-49-x", clone=os.path.join(self.work, "TASK-49", "planted"))
        run = FakeRun({})
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/TASK-49-x: its clone is inside work/", out)
        self.assertEqual(run.calls, [])

    def test_swapped_for_symlink_before_remove_refused(self):
        victim = self.mkw("TASK-50", "TASK-50-x")  # must survive untouched
        wt = self.mkw("TASK-49", "TASK-49-x")
        table = self.table(wt)

        def swap(calls):  # wt is swapped for a symlink while the fetch is on the network
            os.rename(wt, wt + "-moved")
            os.symlink(victim, wt)
            return ok()
        override(table, "fetch", swap)
        run = FakeRun(table)
        gql = gql_for({"TASK-49": (DONE, hist((30, DONE))), "TASK-50": (PROG, hist((30, PROG)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/TASK-49-x: changed since it was checked", out)
        self.assertFalse(run.ran("worktree", "remove"))
        self.assertFalse(run.ran("branch", "-d"))

    def test_symlink_worktree_rejected(self):
        victim = self.mkw("TASK-50", "TASK-50-x")  # must survive untouched
        link = os.path.join(self.work, "TASK-49", "worktrees", "evil")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(victim, link)
        run = FakeRun(self.table(victim))
        gql = gql_for({"TASK-49": (DONE, hist((30, DONE))), "TASK-50": (PROG, hist((30, PROG)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/evil: not a real directory inside TASK-49/, refusing to touch", out)
        self.assertEqual(run.calls, [])

    def test_symlinked_worktrees_dir_rejected(self):
        victim = self.mkw("TASK-50", "TASK-50-x")  # must survive untouched
        link = os.path.join(self.work, "TASK-49", "worktrees")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(os.path.dirname(victim), link)  # work/TASK-49/worktrees -> work/TASK-50/worktrees
        run = FakeRun(self.table(victim))
        gql = gql_for({"TASK-49": (DONE, hist((30, DONE))), "TASK-50": (PROG, hist((30, PROG)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49: worktrees/ is a symlink, refusing to touch", out)
        self.assertEqual(run.calls, [])
        self.assertTrue(os.path.isdir(victim))

    def test_symlinked_issue_dir_rejected(self):
        victim = self.mkw("TASK-50", "TASK-50-x")  # must survive untouched
        os.symlink(os.path.join(self.work, "TASK-50"), os.path.join(self.work, "TASK-49"))
        run = FakeRun(self.table(victim))
        gql = gql_for({"TASK-49": (DONE, hist((30, DONE))), "TASK-50": (PROG, hist((30, PROG)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49: worktrees/ does not resolve inside TASK-49/, refusing to touch", out)
        self.assertEqual(run.calls, [])
        self.assertTrue(os.path.isdir(victim))

    def test_no_upstream_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, upstream=""))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("no upstream", out)

    def test_detached_head_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x", detached=True)
        run = FakeRun(self.table(wt))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("detached HEAD", out)

    def test_unregistered_worktree_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x", register=False)
        run = FakeRun(self.table(wt))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("not a registered worktree", out)

    def test_mixed_worktrees_in_one_issue(self):
        clean = self.mkw("TASK-49", "a")
        dirty = self.mkw("TASK-49", "b")
        run = FakeRun({**self.table(clean), **self.table(dirty, status=" M x.py\0")})
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-49/a", out)
        self.assertIn("prune-skip TASK-49/b: uncommitted changes", out)
        self.assertIn("prune: done (cleaned 1, skipped 1, errors 0)", out)
        self.assertEqual(run.ran("worktree", "remove"), [g(self.clone, "worktree", "remove", os.path.realpath(clean))])

    def test_dry_run_changes_nothing(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        code, out = self.pruner(self.finished("TASK-49"), run, dry=True)
        self.assertEqual(code, 0)
        self.assertIn("prune-plan TASK-49/TASK-49-x", out)
        self.assert_untouched(run)

    def test_transient_git_error_reported(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        table = self.table(wt)
        override(table, "status", OSError("boom"))
        run = FakeRun(table)
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 3)
        self.assertIn("prune-error TASK-49/TASK-49-x", out)

    def test_linear_failure_is_per_issue(self):
        self.mkw("TASK-48", "TASK-48-x")
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        gql = gql_for({"TASK-48": (SystemExit("linear api error: boom"), []), "TASK-49": (DONE, hist((30, DONE)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 3)
        self.assertIn("prune-error TASK-48: Linear: linear api error: boom", out)
        self.assertIn("prune-removed TASK-49/TASK-49-x", out)

    def test_missing_finished_state_fails_loud(self):
        self.mkw("TASK-49", "TASK-49-x")
        gql = gql_for({}, states=("Done", "In Progress"))
        with self.assertRaisesRegex(SystemExit, "not found in Linear: Canceled"):
            self.pruner(gql, FakeRun({}))

    def test_no_worktrees_no_linear_no_output(self):
        os.makedirs(os.path.join(self.work, "TASK-49"))  # a run dir without worktrees
        gql = gql_for({})
        run = FakeRun({})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertEqual(gql.calls, [])
        self.assertEqual(run.calls, [])
        self.assertEqual(out, "")

    def test_unfinished_issue_untouched(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        code, out = self.pruner(gql_for({"TASK-49": (PROG, hist((30, PROG)))}), run)
        self.assertEqual(code, 0)
        self.assertEqual(run.calls, [])
        self.assertEqual(out, "")

    def test_main_dry_run(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        code, out, _ = self.main("--dry-run", gql=self.finished("TASK-49"), run=run)
        self.assertEqual(code, 0)
        self.assertIn("dry-run: prune-plan TASK-49/TASK-49-x", out)
        self.assert_untouched(run)

    def test_main_real_run(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        code, out, _ = self.main(gql=self.finished("TASK-49"), run=run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-49/TASK-49-x", out)

    def test_main_bad_arguments_exit_2(self):
        with self.assertRaises(SystemExit) as cm:
            self.main("--now", gql=gql_for({}), run=FakeRun({}))
        self.assertEqual(cm.exception.code, 2)

    def test_main_linear_error_exit_3(self):
        self.mkw("TASK-49", "TASK-49-x")

        def gql(query, **v):
            raise SystemExit("linear api error: down")
        code, _, err = self.main(gql=gql, run=FakeRun({}))
        self.assertEqual(code, 3)
        self.assertIn("prune.py: transient: linear api error: down", err)

    def test_main_worktree_error_exit_3(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        table = self.table(wt)
        override(table, "status", OSError("boom"))
        code, out, _ = self.main(gql=self.finished("TASK-49"), run=FakeRun(table))
        self.assertEqual(code, 3)
        self.assertIn("prune-error TASK-49/TASK-49-x", out)


if __name__ == "__main__":
    unittest.main()
