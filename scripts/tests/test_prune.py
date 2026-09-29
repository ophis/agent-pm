import io, json, os, shutil, sys, tempfile, unittest
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
        return [c for c in self.calls if c[c.index("-C") + 2:][:len(args)] == args]


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
        self.root = root
        self.work = os.path.join(root, "work")
        self.playground = os.path.join(root, "playground")
        self.clone = os.path.realpath(os.path.join(self.playground, "clone"))
        self.skips = os.path.join(root, "prune-skips.json")
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

    def table(self, wt, branch=None, status="", up=None, remote="ok"):
        """up: (tracking ref, remote, merge ref) of the branch, () for none.
        remote: ok | diverged (lacks our commits) | missing (no such branch) |
        down (unreachable) | unfetched (ahead, with commits not yet in the clone)."""
        path = os.path.realpath(wt)
        branch = branch or os.path.basename(wt)
        upstream, rname, merge = (f"refs/remotes/origin/{branch}", "origin", f"refs/heads/{branch}") if up is None \
            else up or ("", "", "")
        fetch = g(self.clone, "fetch", prune.UPLOAD_PACK, "--", rname, f"+{merge}:{upstream}")
        listed = ok(f"{SHA}\t{merge}\n")
        ls, at_tip, at_ref = {
            "ok": (listed, ok(), ok()),
            "diverged": (listed, ok(code=1), ok(code=1)),
            "missing": (ok(""), ok(), ok()),
            "down": (OSError("net down"), ok(), ok()),
            "unfetched": (listed, ok(code=128, stderr=f"fatal: Not a valid commit name {SHA}"), ok()),
        }[remote]
        return {
            g(self.clone, "worktree", "list", "--porcelain", "-z"): lambda calls: self.listing(),
            g(path, "status", "--porcelain", "--untracked-files=all"): ok(status),
            g(self.clone, "for-each-ref", "--format=%(upstream)%00%(upstream:remotename)%00%(upstream:remoteref)",
              f"refs/heads/{branch}"): ok(f"{upstream}\0{rname}\0{merge}\n"),
            g(self.clone, "ls-remote", prune.UPLOAD_PACK, "--", rname, merge): ls,
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
        p = prune.Pruner(gql, self.cfg, NOW, dry, run=run, work=self.work, playground=self.playground, skips=self.skips)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = p.run()
        return code, buf.getvalue()

    def main(self, *argv, gql, run):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = prune.main(list(argv), gql=gql, run=run, work=self.work, now=NOW,
                              playground=self.playground, skips=self.skips)
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
        self.assertTrue(all(c[:1 + len(prune.SAFE)] == ("git", *prune.SAFE) for c in run.calls))
        self.assertFalse([c for c in run.calls if "--ignored" in c])  # ignored files go with the worktree
        self.assertTrue(run.ran("ls-remote", "--upload-pack=git-upload-pack", "--"))
        self.assertTrue(run.ran("fetch", "--upload-pack=git-upload-pack", "--"))

    def test_fetch_before_ancestry_check_and_remove(self):
        # The fake's tracking ref is stale until fetched: without the fetch first,
        # the ancestry check skips and `branch -d` refuses.
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-49/TASK-49-x", out)
        order = [run.calls.index(run.ran(*a)[0]) for a in
                 (("fetch", prune.UPLOAD_PACK, "--", "origin", "+refs/heads/TASK-49-x:refs/remotes/origin/TASK-49-x"),
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

    def test_remote_name_with_slash(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, up=("refs/remotes/fork/x/feature", "fork/x", "refs/heads/feature")))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("prune-removed TASK-49/TASK-49-x", out)
        self.assertTrue(run.ran("fetch", prune.UPLOAD_PACK, "--", "fork/x", "+refs/heads/feature:refs/remotes/fork/x/feature"))

    def test_unsafe_upstream_never_reaches_the_remote(self):
        a = self.mkw("TASK-49", "a")
        b = self.mkw("TASK-49", "b")
        c = self.mkw("TASK-49", "c")
        d = self.mkw("TASK-49", "d")
        evil = "--upload-pack=/tmp/evil.sh"
        run = FakeRun({**self.table(a, up=(f"refs/remotes/origin/{evil}", "origin", f"refs/heads/{evil}")),
                       **self.table(b, up=("refs/remotes/-c/x", "-c", "refs/heads/x")),
                       **self.table(c, up=("refs/remotes/origin/a..b", "origin", "refs/heads/a..b")),
                       **self.table(d, up=("refs/remotes/origin/-x", "origin", "refs/heads/-x"))})
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("unsafe upstream"), 4)
        self.assertFalse(run.ran("ls-remote"))
        self.assert_untouched(run)

    def test_local_upstream_skipped(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        run = FakeRun(self.table(wt, up=("refs/heads/main", ".", "refs/heads/main")))
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertIn("upstream refs/heads/main is not a remote branch", out)
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

    def test_clone_outside_playground_refused(self):
        for name, clone in (("a", os.path.join(self.work, "TASK-49", "planted")),
                            ("b", os.path.join(self.root, "tmp", "planted")),
                            ("c", os.path.join(self.playground, "nested", "planted"))):
            self.mkw("TASK-49", name, clone=clone)
        run = FakeRun({})
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("is not directly in"), 3)
        self.assertEqual(run.calls, [])

    def test_dotgit_symlink_or_fifo_refused(self):
        real = self.mkw("TASK-50", "TASK-50-x")
        a = os.path.join(self.work, "TASK-49", "worktrees", "a")
        b = os.path.join(self.work, "TASK-49", "worktrees", "b")
        os.makedirs(a)
        os.makedirs(b)
        os.symlink(os.path.join(real, ".git"), os.path.join(a, ".git"))
        os.mkfifo(os.path.join(b, ".git"))  # reading it would block the tick
        run = FakeRun({})
        gql = gql_for({"TASK-49": (DONE, hist((30, DONE))), "TASK-50": (PROG, hist((30, PROG)))})
        code, out = self.pruner(gql, run)
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49/a: not a linked worktree", out)
        self.assertIn("prune-skip TASK-49/b: not a linked worktree", out)
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
        run = FakeRun(self.table(wt, up=()))
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

    def test_undecodable_git_output_is_an_error_not_an_abort(self):
        bad = self.mkw("TASK-49", "a")
        good = self.mkw("TASK-49", "b")
        table = {**self.table(bad), **self.table(good)}
        (key,) = [k for k in table if "for-each-ref" in k and "refs/heads/a" in k]
        table[key] = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        run = FakeRun(table)
        code, out = self.pruner(self.finished("TASK-49"), run)
        self.assertEqual(code, 3)
        self.assertIn("prune-error TASK-49/a", out)
        self.assertIn("prune-removed TASK-49/b", out)

    def test_repeated_skip_logged_once(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        gql = self.finished("TASK-49")
        _, first = self.pruner(gql, FakeRun(self.table(wt, status=" M x.py\n")))
        _, again = self.pruner(gql, FakeRun(self.table(wt, status=" M x.py\n")))
        _, dry = self.pruner(gql, FakeRun(self.table(wt, status=" M x.py\n")), dry=True)
        _, changed = self.pruner(gql, FakeRun(self.table(wt, remote="diverged")))
        self.assertIn("prune-skip TASK-49/TASK-49-x: uncommitted changes", first)
        self.assertEqual(again, "")
        self.assertIn("dry-run: prune-skip TASK-49/TASK-49-x: uncommitted changes", dry)
        self.assertIn("prune-skip TASK-49/TASK-49-x: unpushed commits", changed)
        with open(self.skips) as f:
            self.assertEqual(json.load(f), {"TASK-49/TASK-49-x": "unpushed commits"})
        _, cleaned = self.pruner(gql, FakeRun(self.table(wt)))
        self.assertIn("prune-removed TASK-49/TASK-49-x", cleaned)
        with open(self.skips) as f:
            self.assertEqual(json.load(f), {})

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
