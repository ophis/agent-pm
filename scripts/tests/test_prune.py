import ast, inspect, io, os, shutil, subprocess, sys, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout
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
CFG = {"team": TEAM, "states": dict(IDS_BY_KEY)}
ROLES = {"u-researcher": "researcher", "u-pm": "pm", "u-engineer": "engineer"}
GIT = ("git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")


def gql_for(issues, owners=None, page=50, fail=(), refuse=()):
    """issues: {identifier: (state name, [(hours ago, state name moved to)])}; owners: {identifier: assignee id}.
    Q_FINISHED filters and pages like Linear, archived issues excluded; M_ARCHIVE of fail raises, of refuse fails."""
    owners = owners or {}

    def history(ident):
        return {"nodes": [{"createdAt": (NOW - timedelta(hours=h)).isoformat(), "toStateId": STATE_IDS[s]}
                          for h, s in issues[ident][1]]}

    def gql(query, **v):
        gql.calls.append(query)
        if query == pipeline.Q_TEAM:
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


def msgs(out):
    """Output lines without their timestamps."""
    return [line.split(" ", 2)[2] for line in out.splitlines()]


class FakeGit:
    def __init__(self, results=None):
        """results: {(git subcommand, its first argument): the result to return, or an exception to raise}."""
        self.calls, self.timeouts, self.results = [], [], results or {}

    def __call__(self, argv, timeout):
        self.calls.append(tuple(argv))
        self.timeouts.append(timeout)
        res = self.results.get(tuple(argv[len(GIT) + 2:len(GIT) + 4]), SimpleNamespace(returncode=0, stdout="", stderr=""))
        if isinstance(res, Exception):
            raise res
        return res


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
        self.outside = os.path.join(self.root, "outside")
        os.makedirs(os.path.join(self.outside, "dotgit"))
        write(os.path.join(self.outside, "keep"), "x")
        self.git = FakeGit()

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

    def mkw(self, ident, name, head=None, folder="worktrees"):
        """A linked worktree as git lays it out on disk (files only), on branch <name> by default."""
        wt = os.path.join(self.work, ident, folder, name)
        gitdir = os.path.join(self.clone, ".git", "worktrees", name)
        os.makedirs(wt)
        os.makedirs(gitdir)
        write(os.path.join(wt, ".git"), f"gitdir: {gitdir}\n")
        write(os.path.join(gitdir, "HEAD"), head or f"ref: refs/heads/{name}\n")
        return wt

    def removed(self, wt, branch=None):
        calls = [(*GIT, "-C", self.clone, "worktree", "remove", "--force", "--force", "--", wt)]
        return calls + ([(*GIT, "-C", self.clone, "branch", "-D", "--", branch)] if branch else [])

    def prune(self, gql, team=None, roles=ROLES):
        out = io.StringIO()
        with redirect_stdout(out):
            code = prune.Pruner(gql, CFG, NOW, False, run=self.git, work=self.work, team=team, roles=roles).run()
        return code, out.getvalue()

    def main(self, gql, *argv):
        """prune.main with roles resolved to ROLES; its stderr is self.err."""
        out, self.err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(self.err), mock.patch.object(prune, "load_config", return_value=CFG), \
                mock.patch.object(prune, "runnable"), mock.patch.object(prune, "role_ids", return_value=ROLES):
            code = prune.main(list(argv), gql=gql, run=self.git, work=self.work, now=NOW)
        return code, out.getvalue()

    def fresh(self):
        """An empty work dir and no clone, for the next subTest."""
        shutil.rmtree(self.work)
        shutil.rmtree(self.clone, ignore_errors=True)
        os.makedirs(self.work)

    def test_finished_24h_force_deleted_even_dirty_or_unpushed(self):
        canceled = self.mkw("TASK-37", "TASK-37-x", head="0123abcd\n")
        src_only = self.mkw("TASK-48", "repo48", head="0123abcd\n", folder="src")
        dirty = self.mkw("TASK-49", "TASK-49-dirty")
        write(os.path.join(dirty, "notes.txt"), "uncommitted")
        unpushed = self.mkw("TASK-49", "TASK-49-unpushed")
        src = self.mkw("TASK-49", "repo", head="0123abcd\n", folder="src")
        done = ("Done", [(30, "In Progress"), (24, "Done")])
        gql = gql_for({"TASK-37": ("Canceled", [(50, "Canceled")]), "TASK-48": done, "TASK-49": done},
                      owners={"TASK-49": "u-engineer"})
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(self.git.calls, self.removed(canceled) + self.removed(src_only) + self.removed(dirty, "TASK-49-dirty")
                         + self.removed(unpushed, "TASK-49-unpushed") + self.removed(src))
        self.assertEqual(gql.archived, ["id-TASK-49"])
        self.assertEqual(msgs(out), ["prune-removed TASK-37/TASK-37-x: worktree", "prune-removed TASK-48/src/repo48: worktree",
                                     "prune-removed TASK-49/TASK-49-dirty: worktree",
                                     "prune-removed TASK-49/TASK-49-dirty: local branch TASK-49-dirty",
                                     "prune-removed TASK-49/TASK-49-unpushed: worktree",
                                     "prune-removed TASK-49/TASK-49-unpushed: local branch TASK-49-unpushed",
                                     "prune-removed TASK-49/src/repo: worktree", "prune-archived TASK-49"])

    def test_young_and_in_progress_untouched(self):
        for folder, head in (("worktrees", None), ("src", "0123abcd\n")):
            with self.subTest(folder=folder):
                self.fresh()
                self.mkw("TASK-48", "TASK-48-x", head=head, folder=folder)
                self.mkw("TASK-50", "TASK-50-x", head=head, folder=folder)
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
        for folder, head in (("worktrees", None), ("src", "0123abcd\n")):
            with self.subTest(folder=folder):
                self.fresh()
                victim = self.mkw("TASK-50", "TASK-50-x", head=head, folder=folder)
                base = os.path.join(self.work, "TASK-49", folder)
                os.makedirs(os.path.join(base, "planted"))
                os.symlink(victim, os.path.join(base, "link"))
                write(os.path.join(base, "file"), "x")
                self.mkw("TASK-49", "bad", head="ref: refs/heads/-D\n", folder=folder)
                os.makedirs(os.path.join(self.work, "TASK-48"))
                os.symlink(os.path.dirname(victim), os.path.join(self.work, "TASK-48", folder))
                done = ("Done", [(30, "Done")])
                code, out = self.prune(gql_for({"TASK-48": done, "TASK-49": done, "TASK-50": ("In Progress", [])}))
                self.assertEqual(code, 0)
                self.assertEqual(self.git.calls, [])
                key = "" if folder == "worktrees" else "src/"
                for line in (f"TASK-48/{key}TASK-50-x: not a real directory inside TASK-48/{folder}/, refusing to touch",
                             f"TASK-49/{key}link: not a real directory inside TASK-49/{folder}/, refusing to touch",
                             f"TASK-49/{key}file: not a real directory inside TASK-49/{folder}/, refusing to touch",
                             f"TASK-49/{key}planted: clone unknown: .git is not a gitdir file",
                             f"TASK-49/{key}bad: unsafe branch name"):
                    self.assertIn(f"prune-skip {line}", out)
                self.assertTrue(os.path.isdir(victim))

    def test_git_failures_log_the_error_and_exit_3(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        key = "TASK-49/TASK-49-x"
        failed = lambda err: SimpleNamespace(returncode=1, stdout="", stderr=err)
        timeout = subprocess.TimeoutExpired(["git"], 600)
        cases = (
            (("worktree", "remove"), failed("fatal: cannot remove\n"), "git worktree remove: fatal: cannot remove", False),
            (("worktree", "remove"), timeout, "git worktree remove: TimeoutExpired", False),
            (("branch", "-D"), failed("error: branch not found\n"), "git branch -D: error: branch not found", True),
            (("branch", "-D"), timeout, "git branch -D: TimeoutExpired", True),
        )
        for sub, result, error, worktree_removed in cases:
            with self.subTest(error=error):
                self.git = FakeGit({sub: result})
                code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
                self.assertEqual(code, 3)
                self.assertEqual(self.git.calls, self.removed(wt, "TASK-49-x" if worktree_removed else None))
                self.assertEqual(msgs(out), [f"prune-error {key}: {error}"])

    def test_empty_folder_no_issue_query(self):
        for folder in ("worktrees", "src"):
            with self.subTest(folder=folder):
                self.fresh()
                os.makedirs(os.path.join(self.work, "TASK-49", folder))
                gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
                self.assertEqual(self.prune(gql), (0, ""))
                self.assertNotIn(prune.Q_ISSUE, gql.calls)

    def test_dry_run_changes_nothing(self):
        self.mkw("TASK-49", "TASK-49-x")
        self.mkw("TASK-49", "repo", head="0123abcd\n", folder="src")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
        code, out = self.main(gql, "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(msgs(out), ["dry-run: prune-plan TASK-49/TASK-49-x: force-delete the worktree and local branch TASK-49-x",
                                     "dry-run: prune-plan TASK-49/src/repo: force-delete the worktree",
                                     "dry-run: prune-plan TASK-49: archive"])
        self.assertEqual(self.git.calls, [])
        self.assertNotIn(prune.M_ARCHIVE, gql.calls)

    def test_core_clones_in_src_and_publish_deleted_after_24h(self):
        src, publish = self.mkc("TASK-49", "src", "repo"), self.mkc("TASK-49", "publish")
        busy = self.mkc("TASK-50", "src", "repo")
        gql = gql_for({"TASK-49": ("Done", [(30, "In Progress"), (24, "Done")]), "TASK-50": ("In Progress", [(30, "In Progress")])})
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(msgs(out), ["prune-removed TASK-49/src/repo: clone", "prune-removed TASK-49/publish: clone"])
        self.assertEqual(self.git.calls, [])
        self.assertFalse(os.path.lexists(src))
        self.assertFalse(os.path.lexists(publish))
        self.assertTrue(os.path.isdir(os.path.join(self.work, "TASK-49")))
        self.assertTrue(os.path.isdir(busy))
        self.assertOutsideKept()

    def test_young_core_clones_untouched(self):
        paths = [self.mkc("TASK-48", "src", "repo"), self.mkc("TASK-48", "publish")]
        gql = gql_for({"TASK-48": ("Done", [(50, "Done"), (30, "In Progress"), (24 - 1 / 3600, "Done")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertTrue(all(os.path.isdir(p) for p in paths))

    def test_dry_run_plans_clone_deletion_and_deletes_nothing(self):
        paths = [self.mkc("TASK-49", "src", "repo"), self.mkc("TASK-49", "publish")]
        code, out = self.main(gql_for({"TASK-49": ("Done", [(30, "Done")])}), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(msgs(out), ["dry-run: prune-plan TASK-49/src/repo: delete the clone",
                                     "dry-run: prune-plan TASK-49/publish: delete the clone"])
        self.assertTrue(all(os.path.isdir(os.path.join(p, ".git")) for p in paths))
        self.assertEqual(self.git.calls, [])

    def test_publish_alone_makes_the_issue_a_candidate(self):
        publish = self.mkc("TASK-49", "publish")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql)[0], 0)
        self.assertIn(prune.Q_ISSUE, gql.calls)
        self.assertFalse(os.path.lexists(publish))

    def test_publish_that_is_a_file_is_no_candidate(self):
        os.makedirs(os.path.join(self.work, "TASK-49"))
        write(os.path.join(self.work, "TASK-49", "publish"), "x")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertNotIn(prune.Q_ISSUE, gql.calls)

    def test_clone_with_a_symlinked_dot_git_is_skipped(self):
        for parts in (("src", "repo"), ("publish",)):
            with self.subTest(parts=parts):
                self.fresh()
                path = self.mkc("TASK-49", *parts)
                shutil.rmtree(os.path.join(path, ".git"))
                os.symlink(os.path.join(self.outside, "dotgit"), os.path.join(path, ".git"))
                code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
                self.assertEqual(code, 0)
                self.assertEqual(msgs(out), [f"prune-skip TASK-49/{'/'.join(parts)}: clone unknown: .git is not a gitdir file"])
                self.assertTrue(os.path.isfile(os.path.join(path, "notes.md")))
                self.assertTrue(os.path.isdir(os.path.join(self.outside, "dotgit")))
                self.assertEqual(self.git.calls, [])

    def test_symlinked_clone_entries_refused(self):
        victim = self.mkc("TASK-50", "src", "repo")
        os.makedirs(os.path.join(self.work, "TASK-49", "src"))
        os.symlink(victim, os.path.join(self.work, "TASK-49", "src", "link"))
        os.symlink(victim, os.path.join(self.work, "TASK-49", "publish"))
        os.makedirs(os.path.join(self.work, "TASK-48"))
        os.symlink(os.path.dirname(victim), os.path.join(self.work, "TASK-48", "src"))
        done = ("Done", [(30, "Done")])
        code, out = self.prune(gql_for({"TASK-48": done, "TASK-49": done, "TASK-50": ("In Progress", [])}))
        self.assertEqual(code, 0)
        self.assertEqual(msgs(out), ["prune-skip TASK-48/src/repo: not a real directory inside TASK-48/src/, refusing to touch",
                                     "prune-skip TASK-49/src/link: not a real directory inside TASK-49/src/, refusing to touch",
                                     "prune-skip TASK-49/publish: not a real directory inside TASK-49/, refusing to touch"])
        self.assertTrue(os.path.isdir(os.path.join(victim, ".git")))
        self.assertEqual(self.git.calls, [])

    def test_clone_deletion_failure_logs_the_error_and_exit_3(self):
        src, publish = self.mkc("TASK-49", "src", "repo"), self.mkc("TASK-49", "publish")
        with mock.patch.object(prune.shutil, "rmtree", side_effect=[PermissionError("denied\nby os"), None]) as rmtree:
            code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 3)
        self.assertEqual([c.args for c in rmtree.call_args_list], [(src,), (publish,)])
        self.assertEqual(msgs(out), ["prune-error TASK-49/src/repo: rmtree: PermissionError: denied by os",
                                     "prune-removed TASK-49/publish: clone"])

    def test_archives_pm_and_engineer_issues_finished_24h(self):
        gql = gql_for({"TASK-1": ("Done", [(30, "In Progress"), (24, "Done")]), "TASK-2": ("Canceled", [(50, "Canceled")]),
                       "TASK-3": ("Done", [(30, "Done")]), "TASK-4": ("Canceled", [(24, "Canceled")])},
                      owners={"TASK-1": "u-pm", "TASK-2": "u-pm", "TASK-3": "u-engineer", "TASK-4": "u-engineer"})
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3", "id-TASK-4"])
        self.assertEqual(msgs(out), ["prune-archived TASK-1", "prune-archived TASK-2", "prune-archived TASK-3",
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
        self.assertEqual(msgs(out), ["prune-skip TASK-3: archive: finish time unknown"])
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
        self.assertEqual(msgs(out), ["prune-error TASK-1: archive: linear api error: boom",
                                     "prune-error TASK-2: archive: success: false", "prune-archived TASK-3"])

    def test_archive_reads_every_page_first(self):
        done = ("Done", [(30, "Done")])
        idents = ("TASK-1", "TASK-2", "TASK-3")
        gql = gql_for(dict.fromkeys(idents, done), owners=dict.fromkeys(idents, "u-engineer"), page=2)
        self.assertEqual(self.prune(gql)[0], 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3"])
        self.assertEqual([v["c"] for v in gql.finished], [None, "2"])

    def test_candidate_query_failure_keeps_worktree_step(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        issues = gql_for({"TASK-49": ("Done", [(30, "Done")])})

        def gql(query, **v):
            if query == prune.Q_FINISHED:
                raise SystemExit("linear api error: down")
            return issues(query, **v)
        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertIn("prune-error archive: Linear: linear api error: down", out)
        self.assertEqual(self.git.calls, self.removed(wt, "TASK-49-x"))

    def test_resolves_roles_itself(self):
        gql = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        runs = object()
        with mock.patch.object(prune, "runnable", return_value=runs) as runnable, \
                mock.patch.object(prune, "role_ids", return_value=ROLES) as role_ids:
            self.assertEqual(self.prune(gql, roles=None)[0], 0)
        runnable.assert_called_once_with(CFG)
        role_ids.assert_called_once_with(gql, runs)
        self.assertEqual(gql.archived, ["id-TASK-1"])

        with mock.patch.object(prune, "runnable"), \
                mock.patch.object(prune, "role_ids", side_effect=SystemExit("pipeline.toml [roles.pm]: account 'x' not found in Linear")):
            code, out = self.prune(gql, roles=None)
        self.assertEqual(code, 3)
        self.assertEqual(msgs(out), ["prune-error archive: Linear: pipeline.toml [roles.pm]: account 'x' not found in Linear"])

    def test_given_team_skips_team_query(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        given = pipeline.Team(TEAM, "Team", dict(IDS_BY_KEY))
        self.assertEqual(self.prune(gql, team=given)[0], 0)
        self.assertNotIn(pipeline.Q_TEAM, gql.calls)
        self.assertEqual(self.git.calls, self.removed(wt, "TASK-49-x"))
        self.assertIn("state { id }", prune.Q_ISSUE)

    def test_main_bad_arguments_exit_2_before_anything_runs(self):
        gql = gql_for({})
        with self.assertRaises(SystemExit) as cm:
            self.main(gql, "--bogus")
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("unrecognized arguments: --bogus", self.err.getvalue())
        self.assertEqual((gql.calls, self.git.calls), ([], []))

    def test_main_transient_errors_exit_3(self):
        for exc in (SystemExit("linear api error: down"), subprocess.TimeoutExpired(["curl"], 30), OSError("network down")):
            with self.subTest(exc=repr(exc)):
                def gql(query, **v):
                    raise exc
                self.assertEqual(self.main(gql), (3, ""))
                self.assertEqual(self.err.getvalue(), f"prune.py: transient: {exc}\n")
                self.assertEqual(self.git.calls, [])

    def test_main_config_error_propagates(self):
        with self.assertRaises(SystemExit) as cm:
            self.main(lambda query, **v: {"teams": {"nodes": []}})
        self.assertEqual(cm.exception.code, f"pipeline.toml: team {TEAM} not found in Linear")
        self.assertEqual(self.err.getvalue(), "")

    def test_removes_the_worktree_then_its_branch(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        self.assertEqual(prune.remove_linked(wt, run=self.git), "TASK-49-x")
        self.assertEqual(self.git.calls, self.removed(wt, "TASK-49-x"))
        self.assertEqual(self.git.timeouts, [pipeline.LONG, pipeline.SHORT])

    def test_detached_worktree_has_no_branch_step(self):
        wt = self.mkw("TASK-49", "repo", head="0123abcd\n", folder="src")
        self.assertIsNone(prune.remove_linked(wt, run=self.git))
        self.assertEqual(self.git.calls, self.removed(wt))

    def test_not_a_linked_worktree_raises_before_any_git_call(self):
        plain = self.mkc("TASK-49", "src", "repo")
        with self.assertRaises(ValueError) as cm:
            prune.remove_linked(plain, run=self.git)
        self.assertEqual(str(cm.exception), "clone unknown: .git is not a gitdir file")
        self.assertEqual(self.git.calls, [])

    def test_git_failures_raise_transient(self):
        wt = self.mkw("TASK-49", "TASK-49-x")
        failed = lambda err: SimpleNamespace(returncode=1, stdout="", stderr=err)
        for sub, result, error, steps in (
                (("worktree", "remove"), failed("fatal: cannot remove\n"), "git worktree remove: fatal: cannot remove", 1),
                (("worktree", "remove"), subprocess.TimeoutExpired(["git"], 600), "git worktree remove: TimeoutExpired", 1),
                (("worktree", "remove"), FileNotFoundError("git"), "git worktree remove: FileNotFoundError", 1),
                (("branch", "-D"), failed("error: branch not found\n"), "git branch -D: error: branch not found", 2)):
            with self.subTest(error=error):
                self.git = FakeGit({sub: result})
                with self.assertRaises(prune.TransientError) as cm:
                    prune.remove_linked(wt, run=self.git)
                self.assertEqual(str(cm.exception), error)
                self.assertEqual(len(self.git.calls), steps)

    def test_default_run_is_pipeline_sh_run(self):
        self.assertIs(inspect.signature(prune.remove_linked).parameters["run"].default, pipeline.sh_run)

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
            self.user_queries += query == pipeline.Q_USER
            return (prune_gql if query in (prune.Q_ISSUE, prune.Q_FINISHED, prune.M_ARCHIVE) else linear)(query, **v)
        self.user_queries = 0
        out = io.StringIO()
        with redirect_stdout(out):
            code = promote.main([], gql=gql, now=NOW, config=config,
                                pruner=partial(prune.Pruner, run=self.git, work=self.work))
        return code, linear.issues["DR-1"]["state"], out.getvalue()

    def test_promote_tick_archives_with_its_roles(self):
        gql = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        code, _, out = self.tick(gql)
        self.assertEqual((code, gql.archived), (0, ["id-TASK-1"]))
        self.assertIn("prune-archived TASK-1", out)
        self.assertEqual(self.user_queries, len(tp.ROLE))

    def test_prune_failure_leaves_handoff_alone(self):
        self.mkw("TASK-49", "TASK-49-x")

        def down(query, **v):
            raise SystemExit("linear api error: down")
        code, state, out = self.tick(down)
        self.assertEqual((code, state), (0, "Done"))
        self.assertIn("prune-error TASK-49: Linear: linear api error: down", out)
        self.assertEqual(self.git.calls, [])


class Imports(unittest.TestCase):
    def test_from_pipeline_not_eng(self):
        with open(prune.__file__) as f:
            modules = {n.module for n in ast.walk(ast.parse(f.read())) if isinstance(n, ast.ImportFrom)}
        self.assertNotIn("eng", modules)
        for name in ("sh_run", "SHORT", "LONG", "err_text", "CLONES"):
            self.assertIs(getattr(prune, name), getattr(pipeline, name))


class Locate(unittest.TestCase):
    SHA = "0123456789abcdef0123456789abcdef01234567"

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.clone = os.path.join(self.root, "clone")

    def put(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write(path, text)

    def wt(self, head, name="wt", gitdir=None):
        path = os.path.join(self.root, name)
        gitdir = gitdir or os.path.join(self.clone, ".git", "worktrees", name)
        self.put(os.path.join(path, ".git"), f"gitdir: {gitdir}\n")
        self.put(os.path.join(gitdir, "HEAD"), head)
        return path

    def test_located(self):
        relative = self.wt(self.SHA + "\n", name="relative")
        self.put(os.path.join(relative, ".git"), "gitdir: ../clone/.git/worktrees/relative\n")
        no_head = self.wt("", name="no-head")
        os.remove(os.path.join(self.clone, ".git", "worktrees", "no-head", "HEAD"))
        for path, branch, head in ((self.wt(self.SHA + "\n"), None, self.SHA),
                                   (self.wt("ref: refs/heads/TASK-1-x\n", name="branch"), "TASK-1-x", "ref: refs/heads/TASK-1-x"),
                                   (relative, None, self.SHA), (no_head, None, "")):
            with self.subTest(path=path):
                gitdir = os.path.join(self.clone, ".git", "worktrees", os.path.basename(path))
                self.assertEqual(prune.locate(path), (self.clone, branch, head, gitdir))

    def test_refusals(self):
        self.put(os.path.join(self.root, "dir", ".git", "x"), "")
        self.put(os.path.join(self.root, "text", ".git"), "not a gitdir\n")
        self.put(os.path.join(self.root, "elsewhere", ".git"), f"gitdir: {self.root}/elsewhere/x\n")
        bare = os.path.join(self.root, "clone", "worktrees", "w")
        self.put(os.path.join(bare, "HEAD"), self.SHA)
        self.put(os.path.join(self.root, "bare", ".git"), f"gitdir: {bare}\n")
        for path, reason in (("missing", "clone unknown: .git is not a gitdir file"), ("dir", "clone unknown: .git is not a gitdir file"),
                             ("text", "clone unknown: .git is not a gitdir file"),
                             ("elsewhere", "clone unknown: gitdir is not <clone>/.git/worktrees/<name>"),
                             ("bare", "clone unknown: gitdir is not <clone>/.git/worktrees/<name>")):
            with self.subTest(path=path):
                with self.assertRaises(ValueError) as cm:
                    prune.locate(os.path.join(self.root, path))
                self.assertEqual(str(cm.exception), reason)
        with self.assertRaises(ValueError) as cm:
            prune.locate(self.wt("ref: refs/heads/-D\n", name="bad"))
        self.assertEqual(str(cm.exception), "unsafe branch name")

    def test_read_is_a_stripped_regular_file_or_empty(self):
        self.put(os.path.join(self.root, "f"), " text \n")
        os.mkfifo(os.path.join(self.root, "fifo"))
        for name, text in (("f", "text"), ("fifo", ""), ("missing", "")):
            with self.subTest(name=name):
                self.assertEqual(prune._read(os.path.join(self.root, name)), text)


if __name__ == "__main__":
    unittest.main()
