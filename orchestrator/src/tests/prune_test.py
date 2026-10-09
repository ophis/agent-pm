import ast, io, json, os, shutil, subprocess, sys, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from functools import partial
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402,F401
from board_ids import STATES as IDS_BY_KEY, TEAM, team_node  # noqa: E402
import config, linear, promote, prune, repo  # noqa: E402
import promote_test as tp  # noqa: E402
from attended_test import Tmux  # noqa: E402
from run_fixtures import logged, show  # noqa: E402
from outage import FAILURES, FIELDS, failing  # noqa: E402

NOW = tp.NOW
STATE_IDS = {"Done": IDS_BY_KEY["done"], "Canceled": IDS_BY_KEY["canceled"], "In Progress": IDS_BY_KEY["in_progress"]}
ROLES = {"u-researcher": "researcher", "u-pm": "pm", "u-engineer": "engineer"}
TEAM_OBJ = linear.Team(TEAM, "Team", dict(IDS_BY_KEY))
A, B = "engineer-TASK-7-0b6f2c1e", "dummy-tester-TASK-7-7c1d9e2f"   # TUI sessions of TASK-7


def gql_for(issues, owners=None, page=50, fail=(), refuse=()):
    """issues: {identifier: (state name, [(hours ago, state name moved to)])}; owners: {identifier: assignee id}.
    Q_FINISHED filters and pages like Linear, archived issues excluded; M_ARCHIVE of fail raises, of refuse fails."""
    owners = owners or {}

    def history(ident):
        return {"nodes": [{"createdAt": (NOW - timedelta(hours=h)).isoformat(), "toStateId": STATE_IDS[s]}
                          for h, s in issues[ident][1]]}

    def gql(query, **v):
        gql.calls.append(query)
        if query == linear.Q_TEAM:
            return {"teams": {"nodes": [team_node()]}}
        if query == prune.Q_FINISHED:
            gql.finished.append(v)
            hits = [i for i, (state, _) in issues.items()
                    if i not in gql.gone and owners.get(i) in v["a"] and STATE_IDS[state] in v["s"]]
            start = int(v["c"] or 0)
            return {"issues": {"nodes": [{"id": f"id-{i}", "identifier": i, "history": history(i)}
                                         for i in hits[start:start + page]],
                               "pageInfo": {"hasNextPage": start + page < len(hits), "endCursor": str(start + page)}}}
        if query == prune.M_ARCHIVE:
            gql.archived.append(v["i"])
            ident = v["i"].removeprefix("id-")
            if ident in fail:
                raise SystemExit("linear api error: boom")
            if ident not in refuse:
                gql.gone.add(ident)
            return {"issueArchive": {"success": ident not in refuse}}
        return {"issue": {"state": {"id": STATE_IDS[issues[v["i"]][0]]}, "history": history(v["i"])}}
    gql.calls, gql.finished, gql.archived, gql.gone = [], [], [], set()
    return gql


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class Git:
    """Pruner's git runner: records every call; runs real git when `real`, else fails the test."""

    def __init__(self, real=False):
        self.real, self.calls = real, []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if not self.real:
            raise AssertionError(f"git ran: {argv}")
        return repo.sh(argv, timeout)


def git(*argv):
    return subprocess.run(["git", *argv], check=True, capture_output=True, text=True).stdout.strip()


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        self.outside = os.path.join(self.root, "outside")
        os.makedirs(os.path.join(self.outside, "dotgit"))
        write(os.path.join(self.outside, "keep"), "x")
        self.logs = os.path.join(self.root, "logs")
        p = mock.patch.object(config, "LOGS_DIR", self.logs)
        p.start()
        self.addCleanup(p.stop)

    def mkc(self, ident, *parts):
        """A core clone at work/<ident>/<parts>: a real .git directory, a read-only object file, and a symlink out."""
        path = os.path.join(self.work, ident, *parts)
        os.makedirs(os.path.join(path, ".git", "objects"))
        write(os.path.join(path, ".git", "HEAD"), "ref: refs/heads/main\n")
        pack = os.path.join(path, ".git", "objects", "pack.idx")
        write(pack, "x")
        os.chmod(pack, 0o444)
        write(os.path.join(path, "notes.md"), "uncommitted")
        os.symlink(self.outside, os.path.join(path, "out"))
        return path

    def assertOutsideKept(self):
        self.assertTrue(os.path.isfile(os.path.join(self.outside, "keep")))

    def prune(self, gql, dry=False, tmux=None, git=None, writable=None):
        """(code, the events logged or (dry) copied to stderr, one show() a line)."""
        before, err, out = len(logged(self.logs)), io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            code = prune.Pruner(gql, NOW, dry, work=self.work, team=TEAM_OBJ, roles=ROLES, proc=tmux or Tmux(),
                                run=git or Git(), writable=writable).run()
        self.assertEqual(out.getvalue(), "")  # launchd's stdout is the log too
        copies = [json.loads(line) for line in err.getvalue().splitlines() if line.startswith("{")]
        self.assertEqual({e["src"] for e in logged(self.logs)[before:] + copies} - {"prune"}, set())
        return code, "\n".join(show(e) for e in logged(self.logs)[before:] + copies)

    def mkg(self, ident, *parts, git="file"):
        """An entry at work/<ident>/<parts> whose .git is a file (a linked worktree) or absent (git=None)."""
        path = os.path.join(self.work, ident, *parts)
        os.makedirs(path)
        if git:
            write(os.path.join(path, ".git"), f"gitdir: {self.root}/clone/.git/worktrees/x\n")
        return path

    def fresh(self):
        """An empty work dir, for the next subTest."""
        shutil.rmtree(self.work)
        os.makedirs(self.work)

    def test_entries_that_are_no_clone_or_worktree_are_skipped_and_kept(self):
        for key, parts in (("src/o/bare", ("src", "o", "bare")), ("publish", ("publish",))):
            with self.subTest(key=key):
                self.fresh()
                path = self.mkg("TASK-49", *parts, git=None)
                code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
                self.assertEqual(code, 0)
                self.assertEqual(out.splitlines(), [f"prune-skip entry=TASK-49/{key} reason=not a clone or worktree"])
                self.assertTrue(os.path.isdir(path))

    def test_the_same_skip_twice_is_one_line(self):
        """FR-10: prune-skip once a day."""
        self.mkg("TASK-49", "publish", git=None)
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql), (0, "prune-skip entry=TASK-49/publish reason=not a clone or worktree"))
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(len(logged(self.logs)), 1)

    def test_clones_deleted_beside_skipped_entries(self):
        clone, bare = self.mkc("TASK-49", "src", "o", "repo"), self.mkg("TASK-49", "src", "o", "bare", git=None)
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["prune-skip entry=TASK-49/src/o/bare reason=not a clone or worktree",
                                     "prune-removed entry=TASK-49/src/o/repo what=clone"])
        self.assertFalse(os.path.lexists(clone))
        self.assertTrue(os.path.isdir(bare))

    def test_young_and_in_progress_untouched(self):
        path = self.mkc("TASK-48", "src", "o", "repo")
        busy = self.mkc("TASK-50", "src", "o", "repo")
        gql = gql_for({"TASK-48": ("Done", [(50, "Done"), (30, "In Progress"), (24 - 1 / 3600, "Done")]),
                       "TASK-50": ("In Progress", [(50, "Done"), (30, "In Progress")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertTrue(os.path.isdir(path) and os.path.isdir(busy))

    def test_unknown_finish_time_skipped(self):
        path = self.mkc("TASK-49", "src", "o", "repo")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [])}))
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49 reason=finish time unknown", out)
        self.assertTrue(os.path.isdir(path))

    def test_empty_folder_no_issue_query(self):
        os.makedirs(os.path.join(self.work, "TASK-49", "src"))
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertNotIn(prune.Q_ISSUE, gql.calls)

    def test_core_clones_in_src_and_publish_deleted_after_24h(self):
        src, publish = self.mkc("TASK-49", "src", "o", "repo"), self.mkc("TASK-49", "publish")
        busy = self.mkc("TASK-50", "src", "o", "repo")
        gql = gql_for({"TASK-49": ("Done", [(30, "In Progress"), (24, "Done")]), "TASK-50": ("In Progress", [(30, "In Progress")])})
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["prune-removed entry=TASK-49/src/o/repo what=clone", "prune-removed entry=TASK-49/publish what=clone"])
        self.assertFalse(os.path.lexists(src))
        self.assertFalse(os.path.lexists(publish))
        self.assertTrue(os.path.isdir(os.path.join(self.work, "TASK-49")))
        self.assertTrue(os.path.isdir(busy))
        self.assertOutsideKept()

    def test_dry_run_plans_clone_deletion_and_deletes_nothing(self):
        paths = [self.mkc("TASK-49", "src", "o", "repo"), self.mkc("TASK-49", "publish")]
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
        code, out = self.prune(gql, dry=True)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["prune-plan entry=TASK-49/src/o/repo what=delete the clone",
                                     "prune-plan entry=TASK-49/publish what=delete the clone",
                                     "prune-plan TASK-49 what=archive"])
        self.assertNotIn(prune.M_ARCHIVE, gql.calls)
        self.assertTrue(all(os.path.isdir(os.path.join(p, ".git")) for p in paths))

    def test_dry_run_plans_worktree_deletion_and_touches_nothing(self):
        wt, clone = self.mkg("TASK-49", "src", "o", "n"), self.mkc("TASK-49", "src", "o", "m")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        code, out = self.prune(gql, dry=True)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["prune-plan entry=TASK-49/src/o/m what=delete the clone",
                                     "prune-plan entry=TASK-49/src/o/n what=delete the worktree"])
        self.assertTrue(os.path.isdir(os.path.join(clone, ".git")) and os.path.isfile(os.path.join(wt, ".git")))

    def test_publish_that_is_a_file_is_no_candidate(self):
        os.makedirs(os.path.join(self.work, "TASK-49"))
        write(os.path.join(self.work, "TASK-49", "publish"), "x")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertNotIn(prune.Q_ISSUE, gql.calls)

    def test_clone_with_a_symlinked_dot_git_is_skipped(self):
        for parts in (("src", "o", "n"), ("publish",)):
            with self.subTest(parts=parts):
                self.fresh()
                path = self.mkc("TASK-49", *parts)
                shutil.rmtree(os.path.join(path, ".git"))
                os.symlink(os.path.join(self.outside, "dotgit"), os.path.join(path, ".git"))
                code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
                self.assertEqual(code, 0)
                self.assertEqual(out.splitlines(), [f"prune-skip entry=TASK-49/{'/'.join(parts)} reason=not a clone or worktree"])
                self.assertTrue(os.path.isfile(os.path.join(path, "notes.md")))
                self.assertTrue(os.path.isdir(os.path.join(self.outside, "dotgit")))
        
    def test_symlinked_clone_entries_refused(self):
        victim = self.mkc("TASK-50", "src", "o", "repo")
        os.makedirs(os.path.join(self.work, "TASK-49", "src"))
        os.symlink(victim, os.path.join(self.work, "TASK-49", "src", "link"))
        os.symlink(victim, os.path.join(self.work, "TASK-49", "publish"))
        write(os.path.join(self.work, "TASK-49", "src", "file"), "x")
        os.makedirs(os.path.join(self.work, "TASK-48"))
        os.symlink(os.path.dirname(os.path.dirname(victim)), os.path.join(self.work, "TASK-48", "src"))
        done = ("Done", [(30, "Done")])
        code, out = self.prune(gql_for({"TASK-48": done, "TASK-49": done, "TASK-50": ("In Progress", [])}))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["prune-skip entry=TASK-48/src/o/repo reason=not a real directory inside TASK-48/src/o/, refusing to touch",
                                     "prune-skip entry=TASK-49/src/file reason=not a real directory inside TASK-49/src/, refusing to touch",
                                     "prune-skip entry=TASK-49/src/link reason=not a real directory inside TASK-49/src/, refusing to touch",
                                     "prune-skip entry=TASK-49/publish reason=not a real directory inside TASK-49/, refusing to touch"])
        self.assertTrue(os.path.isdir(os.path.join(victim, ".git")))

    def test_clone_deletion_failure_logs_the_error_and_exit_3(self):
        src, publish = self.mkc("TASK-49", "src", "o", "repo"), self.mkc("TASK-49", "publish")
        with mock.patch.object(prune.shutil, "rmtree", side_effect=[PermissionError("denied\nby os"), None]) as rmtree:
            code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 3)
        self.assertEqual([c.args for c in rmtree.call_args_list], [(src,), (publish,)])
        self.assertEqual(out.splitlines(), ["prune-error entry=TASK-49/src/o/repo error=rmtree: PermissionError: denied by os",
                                     "prune-removed entry=TASK-49/publish what=clone"])

    def test_worktrees_deleted_like_clones_with_uncommitted_work(self):
        a, b = (self.mkg("TASK-49", "src", "o", n) for n in "ab")
        e, gh, pub = self.mkg("TASK-49", "src", "p", "e"), self.mkg("TASK-49", "src", "q", ".github"), self.mkg("TASK-49", "publish")
        write(os.path.join(a, "notes.md"), "uncommitted")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["prune-removed entry=TASK-49/src/o/a what=worktree", "prune-removed entry=TASK-49/src/o/b what=worktree",
                                     "prune-removed entry=TASK-49/src/p/e what=worktree", "prune-removed entry=TASK-49/src/q/.github what=worktree",
                                     "prune-removed entry=TASK-49/publish what=worktree"])
        self.assertFalse(any(os.path.lexists(p) for p in (a, b, e, gh, pub)))
        self.assertFalse(any(os.path.lexists(os.path.join(self.work, "TASK-49", "src", o)) for o in "opq"))
        self.assertTrue(os.path.isdir(os.path.join(self.work, "TASK-49")))

    def test_symlinked_owner_dir_and_owner_entries_refused(self):
        victim = self.mkg("TASK-50", "src", "o", "n")
        os.makedirs(os.path.join(self.work, "TASK-49", "src"))
        os.symlink(os.path.dirname(victim), os.path.join(self.work, "TASK-49", "src", "o"))
        os.makedirs(os.path.join(self.work, "TASK-48", "src", "o"))
        os.symlink(victim, os.path.join(self.work, "TASK-48", "src", "o", "n"))
        write(os.path.join(self.work, "TASK-48", "src", "o", "file"), "x")
        done = ("Done", [(30, "Done")])
        code, out = self.prune(gql_for({"TASK-48": done, "TASK-49": done, "TASK-50": ("In Progress", [])}))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["prune-skip entry=TASK-48/src/o/file reason=not a real directory inside TASK-48/src/o/, refusing to touch",
                                     "prune-skip entry=TASK-48/src/o/n reason=not a real directory inside TASK-48/src/o/, refusing to touch",
                                     "prune-skip entry=TASK-49/src/o reason=not a real directory inside TASK-49/src/, refusing to touch"])
        self.assertTrue(os.path.isfile(os.path.join(victim, ".git")))
        self.assertTrue(os.path.islink(os.path.join(self.work, "TASK-49", "src", "o")))

    def test_emptied_issue_stops_being_a_candidate(self):
        self.mkg("TASK-49", "src", "o", "n")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql)[0], 0)
        self.assertFalse(os.path.lexists(os.path.join(self.work, "TASK-49", "src", "o")))
        again = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(again), (0, ""))
        self.assertNotIn(prune.Q_ISSUE, again.calls)

    def test_owner_dir_with_nothing_to_prune_is_no_candidate(self):
        os.makedirs(os.path.join(self.work, "TASK-49", "src", "o"))
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertNotIn(prune.Q_ISSUE, gql.calls)

    def test_tmp_goes_and_the_rest_of_the_issues_dir_and_logs_stay(self):
        logs = os.path.join(self.root, "logs")
        ident = os.path.join(self.work, "TASK-49")
        os.makedirs(os.path.join(ident, "tmp", "deep"))
        write(os.path.join(ident, "tmp", "deep", "pr.md"), "x")
        os.chmod(os.path.join(ident, "tmp", "deep", "pr.md"), 0o444)
        kept = [os.path.join(ident, f) for f in ("run.jsonl", "writeback.json", "out.md")]
        for f in kept:
            write(f, "x")
        os.makedirs(logs)
        write(os.path.join(logs, "runs.jsonl"), "x")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual((code, out.splitlines()), (0, ["prune-removed entry=TASK-49/tmp what=temp files"]))
        self.assertFalse(os.path.lexists(os.path.join(ident, "tmp")))
        self.assertTrue(all(os.path.isfile(f) for f in kept + [os.path.join(logs, "runs.jsonl")]))

    def test_tmp_dry_run_and_a_symlinked_tmp(self):
        tmp = os.path.join(self.work, "TASK-49", "tmp")
        os.makedirs(tmp)
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}), dry=True)
        self.assertEqual((code, out.splitlines()), (0, ["prune-plan entry=TASK-49/tmp what=delete the temp files"]))
        self.assertTrue(os.path.isdir(tmp))
        os.rmdir(tmp)
        os.symlink(self.outside, tmp)
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual((code, out.splitlines()), (0, ["prune-skip entry=TASK-49/tmp reason=not a real directory inside TASK-49/, refusing to touch"]))
        self.assertOutsideKept()

    def test_dot_files_under_an_owner_are_no_entries(self):
        wt = self.mkg("TASK-49", "src", "o", "n")
        write(os.path.join(self.work, "TASK-49", "src", "o", ".DS_Store"), "x")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual((code, out.splitlines()), (0, ["prune-removed entry=TASK-49/src/o/n what=worktree"]))
        self.assertFalse(os.path.lexists(wt))

    def real_clone(self, at=None):
        """A clone at `at` (default <root>/clone) of a bare origin with one commit on main."""
        seed, bare, clone = os.path.join(self.root, "seed"), os.path.join(self.root, "origin.git"), at or os.path.join(self.root, "clone")
        if not os.path.isdir(bare):
            git("init", "-q", "-b", "main", seed)
            git("-C", seed, "commit", "-q", "--allow-empty", "-m", "one")
            git("clone", "-q", "--bare", seed, bare)
        git("clone", "-q", bare, clone)
        return clone

    def git_env(self):
        return mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                                            "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})

    def test_finished_worktrees_go_with_their_records_and_pushed_branches(self):
        with self.git_env():
            clone = self.real_clone()
            x, y = os.path.join(self.work, "TASK-49", "src", "o", "n-TASK-49"), os.path.join(self.work, "TASK-49", "src", "p", "m-TASK-49")
            git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-x", x, "origin/main")
            git("-C", x, "commit", "-q", "--allow-empty", "-m", "x")
            git("-C", x, "push", "-q", "origin", "TASK-49-x")
            git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-y", y, "origin/main")
            git("-C", y, "commit", "-q", "--allow-empty", "-m", "y")
            git("-C", clone, "branch", "TASK-49-z", "origin/main")
            git("-C", clone, "branch", "TASK-490-q", "origin/main")
            own = os.path.join(self.root, "own")
            git("-C", clone, "worktree", "add", "-q", "-b", "mine", own)
            git("-C", clone, "worktree", "lock", own)
            shutil.move(own, own + "-unmounted")
            write(os.path.join(clone, "dirty.txt"), "x")
            before = [git("-C", clone, *a) for a in (["rev-parse", "HEAD"], ["branch", "--show-current"], ["status", "--porcelain"])]
            run = Git(real=True)
            code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=run, writable=(self.work,))
            self.assertEqual(code, 0, out)
            self.assertEqual(out.splitlines(), ["prune-removed entry=TASK-49/src/o/n-TASK-49 what=worktree",
                                         "prune-removed entry=TASK-49/src/p/m-TASK-49 what=worktree",
                                         f"prune-removed TASK-49 what=branch TASK-49-x in {clone}",
                                         f"prune-skip TASK-49 reason=branch TASK-49-y in {clone} kept: not pushed",
                                         f"prune-removed TASK-49 what=branch TASK-49-z in {clone}"])
            self.assertFalse(os.path.lexists(x) or os.path.lexists(y))
            listing = git("-C", clone, "worktree", "list", "--porcelain")
            self.assertNotIn(x, listing)
            self.assertNotIn(y, listing)
            self.assertIn(f"worktree {own}", listing)
            self.assertEqual(sorted(git("-C", clone, "branch", "--format=%(refname:short)").split()),
                             ["TASK-49-y", "TASK-490-q", "main", "mine"])
            self.assertEqual([git("-C", clone, *a) for a in (["rev-parse", "HEAD"], ["branch", "--show-current"], ["status", "--porcelain"])],
                             before)
            self.assertTrue(run.calls)
            self.assertTrue(all(c[5:8] == ["-C", clone, f"--git-dir={clone}/.git"] for c in run.calls), run.calls)

    def test_a_clone_an_agent_run_could_write_gets_no_git_run(self):
        with self.git_env():
            for name, at, writable in (("in work/", os.path.join(self.work, "TASK-49", "evil"), (self.work,)),
                                       ("in a temp dir, by default", os.path.join(self.root, "tmp-clone"), None)):
                with self.subTest(name):
                    clone = self.real_clone(at)
                    wt = os.path.join(self.work, "TASK-49", "src", "o", "n")
                    git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-x", wt, "origin/main")
                    run = Git(real=True)
                    code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=run, writable=writable)
                    self.assertEqual((code, run.calls), (0, []))
                    self.assertEqual(out.splitlines()[0], "prune-removed entry=TASK-49/src/o/n what=worktree")
                    self.assertTrue(out.splitlines()[1].startswith(f"prune-skip TASK-49 reason={clone} is under "), out)
                    self.assertFalse(os.path.lexists(wt))

    def test_writable_dirs_default_to_work_and_the_temp_dirs(self):
        pruner = prune.Pruner(None, NOW, False, work=self.work, team=TEAM_OBJ, roles=ROLES)
        self.assertEqual(pruner.writable, (self.work, *repo.temp_dirs()))

    def test_a_git_failure_in_the_clone_is_an_error(self):
        with self.git_env():
            clone = self.real_clone()
            git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-x", os.path.join(self.work, "TASK-49", "src", "o", "n"))
        failing = lambda argv, timeout: subprocess.CompletedProcess(argv, 1, "", "fatal: boom")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=failing, writable=(self.work,))
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines()[0], "prune-removed entry=TASK-49/src/o/n what=worktree")
        self.assertTrue(out.splitlines()[1].startswith("prune-error TASK-49 error=RuntimeError: git --git-dir="), out)
        self.assertTrue(out.splitlines()[1].endswith("worktree prune: fatal: boom"), out)

    def test_dry_run_plans_the_clone_step_and_runs_no_git(self):
        with self.git_env():
            clone = self.real_clone()
            wt = os.path.join(self.work, "TASK-49", "src", "o", "n")
            git("-C", clone, "worktree", "add", "-q", "-b", "TASK-49-x", wt)
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}), dry=True, writable=(self.work,))
        self.assertEqual((code, out.splitlines()), (0, ["prune-plan entry=TASK-49/src/o/n what=delete the worktree",
                                                 f"prune-plan TASK-49 what=prune {clone}'s worktree records and pushed TASK-49-* branches"]))
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))

    def test_archives_pm_and_engineer_issues_finished_24h(self):
        gql = gql_for({"TASK-1": ("Done", [(30, "In Progress"), (24, "Done")]), "TASK-2": ("Canceled", [(50, "Canceled")]),
                       "TASK-3": ("Done", [(30, "Done")]), "TASK-4": ("Canceled", [(24, "Canceled")])},
                      owners={"TASK-1": "u-pm", "TASK-2": "u-pm", "TASK-3": "u-engineer", "TASK-4": "u-engineer"})
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3", "id-TASK-4"])
        self.assertEqual(out.splitlines(), ["prune-archived TASK-1", "prune-archived TASK-2", "prune-archived TASK-3",
                                     "prune-archived TASK-4"])

    def test_not_archived(self):
        done = ("Done", [(50, "Done")])
        gql = gql_for({"TASK-1": ("Done", [(24 - 1 / 3600, "Done")]),
                       "TASK-2": ("Done", [(50, "Done"), (30, "In Progress"), (24 - 1 / 3600, "Done")]),
                       "TASK-3": ("Done", []), "TASK-4": ("In Progress", [(50, "Done"), (30, "In Progress")]),
                       "TASK-5": done, "TASK-6": done, "TASK-7": done},
                      owners={"TASK-1": "u-pm", "TASK-2": "u-engineer", "TASK-3": "u-pm", "TASK-4": "u-pm",
                              "TASK-5": "u-researcher", "TASK-6": "u-other", "TASK-7": None})
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, [])
        self.assertEqual(out.splitlines(), ["prune-skip TASK-3 reason=archive: finish time unknown"])
        (v,) = gql.finished
        self.assertEqual(sorted(v["a"]), ["u-engineer", "u-pm"])
        self.assertEqual(sorted(v["s"]), sorted([IDS_BY_KEY["done"], IDS_BY_KEY["canceled"]]))

    def test_archive_failure_others_archived(self):
        done = ("Done", [(30, "Done")])
        gql = gql_for({"TASK-1": done, "TASK-2": done, "TASK-3": done},
                      owners=dict.fromkeys(("TASK-1", "TASK-2", "TASK-3"), "u-pm"), fail={"TASK-1"}, refuse={"TASK-2"})
        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3"])
        self.assertEqual(out.splitlines(), ["prune-error TASK-1 error=archive: SystemExit: linear api error: boom",
                                     "prune-error TASK-2 error=archive: RuntimeError: issueArchive: success: false", "prune-archived TASK-3"])

    def test_archive_reads_every_page_first(self):
        done = ("Done", [(30, "Done")])
        idents = ("TASK-1", "TASK-2", "TASK-3")
        gql = gql_for(dict.fromkeys(idents, done), owners=dict.fromkeys(idents, "u-engineer"), page=2)
        self.assertEqual(self.prune(gql)[0], 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3"])
        self.assertEqual([v["c"] for v in gql.finished], [None, "2"])

    def test_candidate_query_failure_keeps_clone_step(self):
        clone = self.mkc("TASK-49", "src", "o", "repo")
        issues = gql_for({"TASK-49": ("Done", [(30, "Done")])})

        def gql(query, **v):
            if query == prune.Q_FINISHED:
                raise SystemExit("linear api error: down")
            return issues(query, **v)
        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertIn("prune-error error=archive: Linear: SystemExit: linear api error: down", out)
        self.assertFalse(os.path.lexists(clone))

    def test_given_team_skips_team_query(self):
        self.mkc("TASK-49", "src", "o", "repo")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql)[0], 0)
        self.assertNotIn(linear.Q_TEAM, gql.calls)
        self.assertIn("state { id }", prune.Q_ISSUE)

    def test_a_live_tui_session_alone_makes_a_candidate_closed_when_finished_24h(self):
        tmux = Tmux(live=[A, B, "engineer-TASK-8-aaaaaaaa"])
        gql = gql_for({"TASK-7": ("Done", [(24, "Done")]), "TASK-8": ("In Progress", [(30, "In Progress")])})
        code, out = self.prune(gql, tmux=tmux)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [f"prune-closed TASK-7 session={B}", f"prune-closed TASK-7 session={A}"])
        self.assertEqual(gql.calls.count(prune.Q_ISSUE), 2)
        self.assertEqual(list(tmux.live), ["engineer-TASK-8-aaaaaaaa"])

    def test_only_names_of_the_contract_make_candidates_and_get_closed(self):
        others = ["agent-pm-engineer-TASK-7", "Engineer-TASK-8-aaaaaaaa", "engineer-build-7c1d9e2f", "notes"]
        tmux = Tmux(live=[*others, A, "engineer-TASK-70-bbbbbbbb"])
        gql = gql_for({"TASK-7": ("Done", [(30, "Done")]), "TASK-70": ("In Progress", [(30, "In Progress")])})
        code, out = self.prune(gql, tmux=tmux)
        self.assertEqual((code, out.splitlines()), (0, [f"prune-closed TASK-7 session={A}"]))
        self.assertEqual(list(tmux.live), [*others, "engineer-TASK-70-bbbbbbbb"])

    def test_clone_and_session_issue_queried_once_sessions_first(self):
        clone = self.mkc("TASK-7", "src", "o", "repo")
        gql = gql_for({"TASK-7": ("Canceled", [(30, "Canceled")])})
        code, out = self.prune(gql, tmux=Tmux(live=[A]))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [f"prune-closed TASK-7 session={A}", "prune-removed entry=TASK-7/src/o/repo what=clone"])
        self.assertEqual(gql.calls.count(prune.Q_ISSUE), 1)
        self.assertFalse(os.path.lexists(clone))

    def test_young_and_unfinished_sessions_kept(self):
        gql = gql_for({"TASK-7": ("Done", [(24 - 1 / 3600, "Done")]), "TASK-8": ("In Progress", [(30, "In Progress")])})
        tmux = Tmux(live=[A, "engineer-TASK-8-aaaaaaaa"])
        self.assertEqual(self.prune(gql, tmux=tmux), (0, ""))
        self.assertEqual(tmux.tmux_calls(), ["list-sessions"])

    def test_dry_run_plans_each_live_session_and_kills_none(self):
        tmux = Tmux(live=[A, B, "engineer-TASK-70-bbbbbbbb"])
        code, out = self.prune(gql_for({"TASK-7": ("Done", [(30, "Done")]),
                                        "TASK-70": ("In Progress", [(30, "In Progress")])}), dry=True, tmux=tmux)
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [f"prune-plan TASK-7 session={B} what=close the tui session",
                                     f"prune-plan TASK-7 session={A} what=close the tui session"])
        self.assertEqual(tmux.tmux_calls(), ["list-sessions"] * 2)

    def test_a_listing_failure_is_counted_and_the_work_entries_still_pruned(self):
        for dry, plan in ((False, "prune-removed entry=TASK-49/src/o/repo what=clone"),
                          (True, "prune-plan entry=TASK-49/src/o/repo what=delete the clone")):
            with self.subTest(dry=dry):
                clone = self.mkc("TASK-49", "src", "o", "repo")
                tmux = Tmux(listing=(1, "error connecting to /tmp/x (Permission denied)\n"))
                code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}), dry=dry, tmux=tmux)
                error = "TuiError: tmux: error connecting to /tmp/x (Permission denied)"
                self.assertEqual((code, out.splitlines()),
                                 (3, [f"prune-error error=tui: {error}", f"prune-error TASK-49 error={error}", plan]))
                self.assertEqual(os.path.lexists(clone), dry)
                self.fresh()

    def test_kill_failure_counted(self):
        tmux = Tmux(live=[A, B], fail=[B])
        code, out = self.prune(gql_for({"TASK-7": ("Done", [(30, "Done")])}), tmux=tmux)
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines(), [f"prune-error TASK-7 session={B} error=TuiError: tmux: boom",
                                     f"prune-closed TASK-7 session={A}"])
        self.assertEqual(list(tmux.live), [B])

    def tick(self, prune_gql):
        """promote's tick with the real Pruner; (exit code, DR-1's state after Handoff, output); self.events: its events,
        self.err: its stderr."""
        fake = tp.FakeLinear()
        fake.add("DR-1")
        fake.moved("DR-1", 60, "In Review")
        fake.said("DR-1", 45)
        fake.moved("DR-1", 30, "Handoff", frm=tp.STATES["In Review"])
        config = os.path.join(self.root, "config.toml")
        write(config, tp.CONFIG)

        def gql(query, **v):
            self.user_queries += query == linear.Q_USER
            return (prune_gql if query in (prune.Q_ISSUE, prune.Q_FINISHED, prune.M_ARCHIVE) else fake)(query, **v)
        self.user_queries = 0
        before, err = len(logged(self.logs)), io.StringIO()
        with redirect_stderr(err):
            code = promote.main([], gql=gql, now=NOW, config=config,
                                pruner=partial(prune.Pruner, work=self.work, proc=Tmux()))
        self.events, self.err = logged(self.logs)[before:], err.getvalue()
        return code, fake.issues["DR-1"]["state"], "\n".join(show(e) for e in self.events)

    def test_promote_tick_archives_with_its_roles(self):
        gql = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        code, _, out = self.tick(gql)
        self.assertEqual((code, gql.archived), (0, ["id-TASK-1"]))
        self.assertIn("prune-archived TASK-1", out)
        self.assertEqual(self.user_queries, len(tp.ROLE))

    def test_prune_failure_leaves_handoff_alone(self):
        clone = self.mkc("TASK-49", "src", "o", "repo")

        def down(query, **v):
            raise SystemExit("linear api error: down")
        code, state, out = self.tick(down)
        self.assertEqual((code, state), (0, "Done"))
        self.assertIn("prune-error TASK-49 error=Linear: SystemExit: linear api error: down", out)
        self.assertTrue(os.path.isdir(clone))

    def outage(self, op, issues, failed):
        """For each outage.FAILURES, twice: promote's tick, prune's calls of op failing and the rest to issues: exit 0,
        DR-1 promoted, one linear-error (src prune) the first time, none the second; failed: the calls that failed."""
        for name, error in FAILURES.items():
            with self.subTest(failure=name):
                for said in ([("prune", f"linear-error op={op} {FIELDS[name]}")], []):
                    gql = failing(error, gql=issues, ops={op})
                    self.assertEqual(self.tick(gql)[:2], (0, "Done"))
                    self.assertEqual(([(e["src"], show(e)) for e in self.events], self.err),
                                     ([("promote", "promote DR-1 to=C-1"), *said], ""))
                    self.assertEqual(gql.failed, failed)

    def test_an_outage_reading_an_issue_stops_prune(self):
        clones = [self.mkc(ident, "src", "o", "repo") for ident in ("TASK-48", "TASK-49")]
        issues = gql_for(dict.fromkeys(("TASK-48", "TASK-49"), ("Done", [(30, "Done")])))
        self.outage("issue", issues, [(prune.Q_ISSUE, {"i": "TASK-48"})])
        self.assertEqual(issues.calls, [])
        self.assertTrue(all(os.path.isdir(c) for c in clones))

    def test_an_outage_reading_the_finished_issues_stops_prune(self):
        issues = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        self.outage("issues", issues, [(prune.Q_FINISHED, mock.ANY)])

    def test_an_outage_archiving_stops_prune(self):
        done = ("Done", [(30, "Done")])
        issues = gql_for({"TASK-1": done, "TASK-2": done}, owners={"TASK-1": "u-pm", "TASK-2": "u-pm"})
        self.outage("issueArchive", issues, [(prune.M_ARCHIVE, {"i": "id-TASK-1"})])


class Imports(unittest.TestCase):
    def test_from_pipeline_not_eng(self):
        with open(prune.__file__) as f:
            modules = {n.module for n in ast.walk(ast.parse(f.read())) if isinstance(n, ast.ImportFrom)}
        self.assertNotIn("eng", modules)
        self.assertIs(prune.CLONES, config.CLONES)


if __name__ == "__main__":
    unittest.main()
