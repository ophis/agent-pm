import ast, io, os, shutil, sys, tempfile, unittest
from contextlib import redirect_stdout
from datetime import timedelta
from functools import partial
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import STATES as IDS_BY_KEY, TEAM, team_node  # noqa: E402
import config, linear, promote, prune  # noqa: E402
import promote_test as tp  # noqa: E402

NOW = tp.NOW
STATE_IDS = {"Done": IDS_BY_KEY["done"], "Canceled": IDS_BY_KEY["canceled"], "In Progress": IDS_BY_KEY["in_progress"]}
ROLES = {"u-researcher": "researcher", "u-pm": "pm", "u-engineer": "engineer"}
TEAM_OBJ = linear.Team(TEAM, "Team", dict(IDS_BY_KEY))


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


def msgs(out):
    """Output lines without their timestamps."""
    return [line.split(" ", 2)[2] for line in out.splitlines()]


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        self.outside = os.path.join(self.root, "outside")
        os.makedirs(os.path.join(self.outside, "dotgit"))
        write(os.path.join(self.outside, "keep"), "x")

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

    def prune(self, gql, dry=False):
        out = io.StringIO()
        with redirect_stdout(out):
            code = prune.Pruner(gql, NOW, dry, work=self.work, team=TEAM_OBJ, roles=ROLES).run()
        return code, out.getvalue()

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

    def test_entries_without_a_git_directory_are_skipped_and_kept(self):
        for key, parts, git in (("src/linked", ("src", "linked"), "file"), ("src/bare", ("src", "bare"), None),
                                ("publish", ("publish",), "file"), ("publish", ("publish",), None)):
            with self.subTest(key=key, git=git):
                self.fresh()
                path = self.mkg("TASK-49", *parts, git=git)
                code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
                self.assertEqual(code, 0)
                self.assertEqual(msgs(out), [f"prune-skip TASK-49/{key}: not a clone: .git is not a directory"])
                self.assertTrue(os.path.isdir(path))

    def test_clones_deleted_beside_skipped_entries(self):
        clone, linked = self.mkc("TASK-49", "src", "repo"), self.mkg("TASK-49", "src", "wt")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 0)
        self.assertEqual(msgs(out), ["prune-removed TASK-49/src/repo: clone",
                                     "prune-skip TASK-49/src/wt: not a clone: .git is not a directory"])
        self.assertFalse(os.path.lexists(clone))
        self.assertTrue(os.path.isdir(linked))

    def test_young_and_in_progress_untouched(self):
        path = self.mkc("TASK-48", "src", "repo")
        busy = self.mkc("TASK-50", "src", "repo")
        gql = gql_for({"TASK-48": ("Done", [(50, "Done"), (30, "In Progress"), (24 - 1 / 3600, "Done")]),
                       "TASK-50": ("In Progress", [(50, "Done"), (30, "In Progress")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertTrue(os.path.isdir(path) and os.path.isdir(busy))

    def test_unknown_finish_time_skipped(self):
        path = self.mkc("TASK-49", "src", "repo")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [])}))
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49: finish time unknown", out)
        self.assertTrue(os.path.isdir(path))

    def test_unsafe_entries_refused(self):
        victim = self.mkc("TASK-50", "src", "repo")
        base = os.path.join(self.work, "TASK-49", "src")
        os.makedirs(os.path.join(base, "planted"))
        os.symlink(victim, os.path.join(base, "link"))
        write(os.path.join(base, "file"), "x")
        os.makedirs(os.path.join(self.work, "TASK-48"))
        os.symlink(os.path.dirname(victim), os.path.join(self.work, "TASK-48", "src"))
        done = ("Done", [(30, "Done")])
        code, out = self.prune(gql_for({"TASK-48": done, "TASK-49": done, "TASK-50": ("In Progress", [])}))
        self.assertEqual(code, 0)
        for line in ("TASK-48/src/repo: not a real directory inside TASK-48/src/, refusing to touch",
                     "TASK-49/src/link: not a real directory inside TASK-49/src/, refusing to touch",
                     "TASK-49/src/file: not a real directory inside TASK-49/src/, refusing to touch",
                     "TASK-49/src/planted: not a clone: .git is not a directory"):
            self.assertIn(f"prune-skip {line}", out)
        self.assertTrue(os.path.isdir(victim))

    def test_empty_folder_no_issue_query(self):
        os.makedirs(os.path.join(self.work, "TASK-49", "src"))
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertNotIn(prune.Q_ISSUE, gql.calls)

    def test_core_clones_in_src_and_publish_deleted_after_24h(self):
        src, publish = self.mkc("TASK-49", "src", "repo"), self.mkc("TASK-49", "publish")
        busy = self.mkc("TASK-50", "src", "repo")
        gql = gql_for({"TASK-49": ("Done", [(30, "In Progress"), (24, "Done")]), "TASK-50": ("In Progress", [(30, "In Progress")])})
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(msgs(out), ["prune-removed TASK-49/src/repo: clone", "prune-removed TASK-49/publish: clone"])
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
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
        code, out = self.prune(gql, dry=True)
        self.assertEqual(code, 0)
        self.assertEqual(msgs(out), ["dry-run: prune-plan TASK-49/src/repo: delete the clone",
                                     "dry-run: prune-plan TASK-49/publish: delete the clone",
                                     "dry-run: prune-plan TASK-49: archive"])
        self.assertNotIn(prune.M_ARCHIVE, gql.calls)
        self.assertTrue(all(os.path.isdir(os.path.join(p, ".git")) for p in paths))

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
                self.assertEqual(msgs(out), [f"prune-skip TASK-49/{'/'.join(parts)}: not a clone: .git is not a directory"])
                self.assertTrue(os.path.isfile(os.path.join(path, "notes.md")))
                self.assertTrue(os.path.isdir(os.path.join(self.outside, "dotgit")))
        
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

    def test_candidate_query_failure_keeps_clone_step(self):
        clone = self.mkc("TASK-49", "src", "repo")
        issues = gql_for({"TASK-49": ("Done", [(30, "Done")])})

        def gql(query, **v):
            if query == prune.Q_FINISHED:
                raise SystemExit("linear api error: down")
            return issues(query, **v)
        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertIn("prune-error archive: Linear: linear api error: down", out)
        self.assertFalse(os.path.lexists(clone))

    def test_given_team_skips_team_query(self):
        self.mkc("TASK-49", "src", "repo")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql)[0], 0)
        self.assertNotIn(linear.Q_TEAM, gql.calls)
        self.assertIn("state { id }", prune.Q_ISSUE)

    def tick(self, prune_gql):
        """promote's tick with the real Pruner; (exit code, DR-1's state after Handoff, output)."""
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
        out = io.StringIO()
        with redirect_stdout(out):
            code = promote.main([], gql=gql, now=NOW, config=config,
                                pruner=partial(prune.Pruner, work=self.work))
        return code, fake.issues["DR-1"]["state"], out.getvalue()

    def test_promote_tick_archives_with_its_roles(self):
        gql = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        code, _, out = self.tick(gql)
        self.assertEqual((code, gql.archived), (0, ["id-TASK-1"]))
        self.assertIn("prune-archived TASK-1", out)
        self.assertEqual(self.user_queries, len(tp.ROLE))

    def test_prune_failure_leaves_handoff_alone(self):
        clone = self.mkc("TASK-49", "src", "repo")

        def down(query, **v):
            raise SystemExit("linear api error: down")
        code, state, out = self.tick(down)
        self.assertEqual((code, state), (0, "Done"))
        self.assertIn("prune-error TASK-49: Linear: linear api error: down", out)
        self.assertTrue(os.path.isdir(clone))


class Imports(unittest.TestCase):
    def test_from_pipeline_not_eng(self):
        with open(prune.__file__) as f:
            modules = {n.module for n in ast.walk(ast.parse(f.read())) if isinstance(n, ast.ImportFrom)}
        self.assertNotIn("eng", modules)
        self.assertIs(prune.CLONES, config.CLONES)


if __name__ == "__main__":
    unittest.main()
