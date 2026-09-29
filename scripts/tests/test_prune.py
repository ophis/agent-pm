import io, os, shutil, sys, tempfile, unittest
from contextlib import redirect_stdout
from datetime import timedelta
from functools import partial
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import STATES as IDS_BY_KEY, TEAM, team_node  # noqa: E402
import pipeline, promote, prune  # noqa: E402
import test_promote as tp  # noqa: E402

NOW = tp.NOW
STATE_IDS = {"Done": IDS_BY_KEY["done"], "Canceled": IDS_BY_KEY["canceled"], "In Progress": IDS_BY_KEY["in_progress"]}


def gql_for(issues):
    """issues: {identifier: (state name, [(hours ago, state name moved to)])}."""
    def gql(query, **v):
        gql.calls.append(query)
        if query == pipeline.Q_TEAM:
            return {"teams": {"nodes": [team_node()]}}
        state, moves = issues[v["i"]]
        return {"issue": {"state": {"id": STATE_IDS[state]}, "history": {"nodes": [
            {"createdAt": (NOW - timedelta(hours=h)).isoformat(), "toStateId": STATE_IDS[s]} for h, s in moves]}}}
    gql.calls = []
    return gql


class FakeGit:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(tuple(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.work = os.path.join(self.root, "work")
        self.clone = os.path.join(self.root, "clone")
        os.makedirs(self.work)
        self.git = FakeGit()

    def mkw(self, ident, name, head=None):
        """A linked worktree as git lays it out on disk (files only), on branch <name> by default."""
        wt = os.path.join(self.work, ident, "worktrees", name)
        gitdir = os.path.join(self.clone, ".git", "worktrees", name)
        os.makedirs(wt)
        os.makedirs(gitdir)
        write(os.path.join(wt, ".git"), f"gitdir: {gitdir}\n")
        write(os.path.join(gitdir, "HEAD"), head or f"ref: refs/heads/{name}\n")
        return wt

    def removed(self, wt, branch=None):
        calls = [("git", "-C", self.clone, "worktree", "remove", "--force", "--force", "--", wt)]
        return calls + ([("git", "-C", self.clone, "branch", "-D", "--", branch)] if branch else [])

    def prune(self, gql, team=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = prune.Pruner(gql, {"team": TEAM, "states": dict(IDS_BY_KEY)}, NOW, False, run=self.git, work=self.work,
                                team=team).run()
        return code, out.getvalue()

    def test_done_24h_force_deleted_even_dirty_or_unpushed(self):
        dirty = self.mkw("TASK-49", "TASK-49-dirty")
        write(os.path.join(dirty, "notes.txt"), "uncommitted")
        unpushed = self.mkw("TASK-49", "TASK-49-unpushed")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "In Progress"), (24, "Done")])}))
        self.assertEqual(code, 0)
        self.assertEqual(self.git.calls, self.removed(dirty, "TASK-49-dirty") + self.removed(unpushed, "TASK-49-unpushed"))
        self.assertIn("prune-removed TASK-49/TASK-49-dirty: worktree", out)
        self.assertIn("prune-removed TASK-49/TASK-49-unpushed: local branch TASK-49-unpushed", out)

    def test_canceled_24h_cleaned_detached_has_no_branch(self):
        wt = self.mkw("TASK-37", "TASK-37-x", head="0123abcd\n")
        code, _ = self.prune(gql_for({"TASK-37": ("Canceled", [(50, "Canceled")])}))
        self.assertEqual(code, 0)
        self.assertEqual(self.git.calls, self.removed(wt))

    def test_young_and_in_progress_untouched(self):
        self.mkw("TASK-48", "TASK-48-x")
        self.mkw("TASK-50", "TASK-50-x")
        gql = gql_for({"TASK-48": ("Done", [(50, "Done"), (30, "In Progress"), (24 - 1 / 3600, "Done")]),
                       "TASK-50": ("In Progress", [(50, "Done"), (30, "In Progress")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(self.git.calls, [])

    def test_unknown_finish_time_skipped(self):
        self.mkw("TASK-49", "TASK-49-x")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [])}))
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49: finish time unknown", out)
        self.assertEqual(self.git.calls, [])

    def test_unsafe_entries_refused(self):
        victim = self.mkw("TASK-50", "TASK-50-x")
        base = os.path.join(self.work, "TASK-49", "worktrees")
        os.makedirs(os.path.join(base, "planted", ".git"))
        os.symlink(victim, os.path.join(base, "link"))
        self.mkw("TASK-49", "bad", head="ref: refs/heads/-D\n")
        os.makedirs(os.path.join(self.work, "TASK-48"))
        os.symlink(os.path.dirname(victim), os.path.join(self.work, "TASK-48", "worktrees"))
        done = ("Done", [(30, "Done")])
        code, out = self.prune(gql_for({"TASK-48": done, "TASK-49": done, "TASK-50": ("In Progress", [])}))
        self.assertEqual(code, 0)
        self.assertEqual(self.git.calls, [])
        for line in ("TASK-48/TASK-50-x: not a real directory", "TASK-49/link: not a real directory",
                     "TASK-49/planted: clone unknown", "TASK-49/bad: unsafe branch name"):
            self.assertIn(f"prune-skip {line}", out)
        self.assertTrue(os.path.isdir(victim))

    def test_dry_run_changes_nothing(self):
        self.mkw("TASK-49", "TASK-49-x")
        out = io.StringIO()
        cfg = {"team": TEAM, "states": dict(IDS_BY_KEY)}
        with redirect_stdout(out), mock.patch.object(prune, "load_config", return_value=cfg):
            code = prune.main(["--dry-run"], gql=gql_for({"TASK-49": ("Done", [(30, "Done")])}),
                              run=self.git, work=self.work, now=NOW)
        self.assertEqual(code, 0)
        self.assertIn("dry-run: prune-plan TASK-49/TASK-49-x: force-delete the worktree and local branch TASK-49-x",
                      out.getvalue())
        self.assertEqual(self.git.calls, [])

    def test_no_worktree_no_linear(self):
        os.makedirs(os.path.join(self.work, "TASK-49", "worktrees"))
        gql = gql_for({})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(gql.calls, [])

    def test_given_team_skips_team_query(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        given = pipeline.Team(TEAM, "Team", {}, dict(IDS_BY_KEY))
        self.assertEqual(self.prune(gql, team=given)[0], 0)
        self.assertNotIn(pipeline.Q_TEAM, gql.calls)
        self.assertEqual(self.git.calls, self.removed(wt, "TASK-49-x"))

    def test_resolves_team_itself(self):
        self.mkw("TASK-49", "TASK-49-x")
        gql = gql_for({"TASK-49": ("Canceled", [(30, "Canceled")])})
        self.prune(gql)
        self.assertEqual(gql.calls[0], pipeline.Q_TEAM)
        self.assertIn("state { id }", prune.Q_ISSUE)

    def tick(self, prune_gql):
        """promote's tick with the real Pruner; (exit code, DR-1's state after Handoff, output)."""
        linear = tp.FakeLinear()
        linear.add("DR-1")
        linear.moved("DR-1", 60, "In Review")
        linear.said("DR-1", 45)
        linear.moved("DR-1", 30, "Handoff", frm=tp.STATES["In Review"])
        config = os.path.join(self.root, "pipeline.toml")
        write(config, tp.CONFIG)

        def gql(query, **v):
            return (prune_gql if query == prune.Q_ISSUE else linear)(query, **v)
        out = io.StringIO()
        with redirect_stdout(out):
            code = promote.main([], gql=gql, now=NOW, config=config,
                                pruner=partial(prune.Pruner, run=self.git, work=self.work))
        return code, linear.issues["DR-1"]["state"], out.getvalue()

    def test_promote_tick_prunes(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        self.assertEqual(self.tick(gql_for({"TASK-49": ("Done", [(30, "Done")])}))[:2], (0, "Done"))
        self.assertEqual(self.git.calls, self.removed(wt, "TASK-49-x"))

    def test_prune_failure_leaves_handoff_alone(self):
        self.mkw("TASK-49", "TASK-49-x")

        def down(query, **v):
            raise SystemExit("linear api error: down")
        code, state, out = self.tick(down)
        self.assertEqual((code, state), (0, "Done"))
        self.assertIn("prune-error TASK-49: Linear: linear api error: down", out)
        self.assertEqual(self.git.calls, [])


if __name__ == "__main__":
    unittest.main()
