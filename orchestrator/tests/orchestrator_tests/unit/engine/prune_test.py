"""Orchestrator prune behavior at its dependency boundaries."""

import io
import json
import os
import shutil
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from unittest import mock

from agent_pm.delegate import ConfigError, SessionError
from agent_pm.orchestrator.engine import config, prune
from agent_pm.orchestrator.engine.types import LinearError
from agent_pm.orchestrator.trackers.linear import linear_tracker, ops
from agent_pm.utils import temp_dirs
from orchestrator_tests.process_fixtures import CompletedProcess
from orchestrator_tests.prune_fixtures import (
    NOW,
    ROLES,
    STATE_IDS,
    TEAM_OBJ,
    PruneDelegate,
    PruneScenario,
    gql_for,
    tree,
    write,
)
from orchestrator_tests.unit import promote_fixtures as tp
from orchestrator_tests.unit.board_ids import FINISHED
from orchestrator_tests.unit.run_fixtures import CORE_LOCAL, HARNESS, logged, show

from delegate.tests.workdir import checkout_path

A, B = "engineer-TASK-7-0b6f2c1e", "dummy-tester-TASK-7-7c1d9e2f"
SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
OTHER_SID = "7c1d9e2f-0a3b-4c5d-8e6f-1a2b3c4d5e6f"


class RepositoryCleanup(PruneScenario):
    def test_prune_cleans_through_the_delegate(self):
        self.mkc("TASK-49")
        self.mkg("TASK-49", "o", "wt")
        beside = os.path.join(self.work, "TASK-49", "notes.md")
        write(beside, "x")
        busy = self.mkc("TASK-50")
        gql = gql_for(
            {
                "TASK-49": ("Done", [(30, "In Progress"), (2, "Done")]),
                "TASK-50": ("In Progress", [(30, "In Progress")]),
            }
        )
        code, out = self.prune(gql)
        self.assertEqual((code, out.splitlines()), (0, ["prune-removed TASK-49 what=agent dir"]))
        self.assertIsNone(self.checkouts("TASK-49"))
        self.assertTrue(os.path.isfile(beside))
        self.assertTrue(os.path.isdir(busy))
        self.assertOutsideKept()
        self.assertNotIn(ops.Q_TEAM, gql.calls)
        self.assertIn("state { id }", linear_tracker.Q_ISSUE)

    def test_a_tick_reads_the_runs_log_once(self):
        idents = [f"TASK-{n}" for n in range(40, 46)]
        for ident in idents:
            self.mkc(ident)
        real, reads = open, []

        def counted(path, *args, **kwargs):
            reads.append(path)
            return real(path, *args, **kwargs)

        gql = gql_for(dict.fromkeys(idents, ("Done", [(30, "Done")])))
        with mock.patch("builtins.open", side_effect=counted):
            code, out = self.prune(gql)
        self.assertEqual(len(out.splitlines()), len(idents), out)
        self.assertEqual((code, reads.count(self.cfg.runs_log)), (0, 1))

    def test_clean_refusal_is_a_skip_once_and_cleans_no_clone(self):
        self.fake_clone()
        wt = self.mkg("TASK-49")
        refusal = ConfigError("not a real directory: x, refusing to touch")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        with mock.patch.object(PruneDelegate, "clean", side_effect=refusal) as clean:
            for out in (
                "prune-skip TASK-49 reason=ConfigError: not a real directory: x, refusing to touch",
                "",
            ):
                self.assertEqual(self.prune(gql, writable=(self.work,)), (0, out))
        self.assertEqual(clean.call_args.args, (self.cfg.issue_workdir("TASK-49"),))
        self.assertEqual(clean.call_args.kwargs, {"dry": False})
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))

    def test_a_session_error_in_clean_is_an_error(self):
        self.fake_clone()
        wt = self.mkg("TASK-49")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])})
        with mock.patch.object(PruneDelegate, "clean", side_effect=SessionError("host gone")):
            self.assertEqual(
                self.prune(gql, writable=(self.work,)),
                (3, "prune-error TASK-49 error=clean: SessionError: host gone"),
            )
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))

    def test_prune_uses_the_recorded_delegate_kind(self):
        class Recorded(PruneDelegate):
            cleaned = []

            @classmethod
            def clean(cls, workdir, *, dry=False, scratch=False):
                cls.cleaned.append(workdir)
                return super().clean(workdir, dry=dry, scratch=scratch)

        self.record("TASK-7", SID, [A])
        self.record("TASK-8", OTHER_SID, [B], agent=False)
        with open(self.cfg.runs_log) as f:
            rows = f.read().replace('"delegate_kind": "harness"', '"delegate_kind": "recorded"')
        write(self.cfg.runs_log, rows)
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(30, "Done")])))
        self.cfg = replace(self.cfg, delegates={"harness": PruneDelegate})
        unknown = (
            "prune-skip TASK-7 reason=ConfigError: delegate kind 'recorded' is no longer configured"
        )
        self.assertEqual(self.prune(gql), (0, unknown))
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(self.checkouts("TASK-7"), ())
        self.cfg = replace(self.cfg, delegates={"harness": PruneDelegate, "recorded": Recorded})
        self.assertEqual(
            self.prune(gql),
            (0, f"prune-closed TASK-7 session={SID}\nprune-removed TASK-7 what=agent dir"),
        )
        self.assertEqual(Recorded.cleaned, [self.cfg.issue_workdir("TASK-7")])

    def test_pruned_issue_is_left_alone(self):
        self.record("TASK-7", SID, [A])
        gql = gql_for({"TASK-7": ("Done", [(30, "Done")])})
        self.assertEqual(
            self.prune(gql),
            (0, f"prune-closed TASK-7 session={SID}\nprune-removed TASK-7 what=agent dir"),
        )
        PruneDelegate.attach(self.cfg.issue_workdir("TASK-7"))
        self.record("TASK-8", OTHER_SID, [B], agent=False)
        again = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(30, "Done")])))
        with mock.patch.object(PruneDelegate, "attach", side_effect=AssertionError("attached")):
            self.assertEqual(self.prune(again), (0, ""))
            self.assertEqual(self.prune(again), (0, ""))
        self.assertEqual(again.calls.count(linear_tracker.Q_ISSUE), 4)
        self.assertEqual(self.closed(), [A])

    def test_young_workdirs_without_agent_files_are_queried_and_kept(self):
        old = os.path.join(self.work, "TASK-49", "src", "o", "repo")
        os.makedirs(os.path.join(old, ".git"))
        self.record("TASK-48", OTHER_SID, [B], agent=False)
        os.makedirs(checkout_path(os.path.join(self.work, "notes"), "o", "repo", "notes"))
        gql = gql_for(dict.fromkeys(("TASK-48", "TASK-49"), ("Done", [(30, "Done")])))
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(gql.calls.count(linear_tracker.Q_ISSUE), 2)
        self.assertTrue(os.path.isdir(os.path.join(old, ".git")))

    def test_young_and_in_progress_untouched(self):
        path = self.mkc("TASK-48")
        busy = self.mkc("TASK-50")
        self.api.live = ["engineer-TASK-48-aaaaaaaa", "engineer-TASK-50-bbbbbbbb"]
        gql = gql_for(
            {
                "TASK-48": ("Done", [(50, "Done"), (30, "In Progress"), (2 - 1 / 3600, "Done")]),
                "TASK-50": ("In Progress", [(50, "Done"), (30, "In Progress")]),
            }
        )
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertTrue(os.path.isdir(path) and os.path.isdir(busy))
        self.assertEqual(self.api.order, [])

    def test_unknown_finish_time_skipped(self):
        path = self.mkc("TASK-49")
        code, out = self.prune(gql_for({"TASK-49": ("Done", [])}))
        self.assertEqual(code, 0)
        self.assertIn("prune-skip TASK-49 reason=finish time unknown", out)
        self.assertTrue(os.path.isdir(path))

    def test_worktree_clones_are_cleaned_once_after_the_agent_files_go(self):
        clone = self.fake_clone()
        for owner, name in (("o", "a"), ("p", "b")):
            self.mkg("TASK-49", owner, name)
        write(os.path.join(self.mkg("TASK-49", "o", "c"), "notes.md"), "uncommitted")
        calls = []

        def git(argv, _timeout):
            calls.append((self.checkouts("TASK-49"), argv))
            return CompletedProcess(argv, 0, "", "")

        code, out = self.prune(
            gql_for({"TASK-49": ("Done", [(30, "Done")])}), git=git, writable=(self.work,)
        )
        self.assertEqual((code, out.splitlines()), (0, ["prune-removed TASK-49 what=agent dir"]))
        self.assertEqual(
            [argv[-2:] for _, argv in calls if "worktree" in argv], [["worktree", "prune"]]
        )
        self.assertEqual(
            {(there, tuple(argv[5:8])) for there, argv in calls},
            {(None, ("-C", clone, f"--git-dir={clone}/.git"))},
        )

    def test_a_failed_clean_logs_the_error_and_cleans_no_clone(self):
        self.fake_clone()
        kept = self.mkg("TASK-48")
        self.mkc("TASK-49")
        with mock.patch.object(
            shutil, "rmtree", side_effect=[PermissionError("denied\nby os"), None]
        ) as rmtree:
            code, out = self.prune(
                gql_for(dict.fromkeys(("TASK-48", "TASK-49"), ("Done", [(30, "Done")]))),
                writable=(self.work,),
            )
        self.assertEqual((code, rmtree.call_count), (3, 2))
        self.assertEqual(
            out.splitlines(),
            [
                "prune-error TASK-48 error=clean: PermissionError: denied by os",
                "prune-removed TASK-49 what=agent dir",
            ],
        )
        self.assertTrue(os.path.isfile(os.path.join(kept, ".git")))

    def test_dry_run_plans_and_touches_nothing(self):
        clone = self.fake_clone()
        self.record("TASK-7", SID, [A])
        wt = self.mkg("TASK-7")
        gql = gql_for({"TASK-7": ("Done", [(30, "Done")])}, owners={"TASK-7": "u-pm"})
        with mock.patch.object(PruneDelegate, "clean", side_effect=AssertionError("cleaned")):
            code, out = self.prune(gql, dry=True, writable=(self.work,))
        self.assertEqual(
            (code, out.splitlines()),
            (
                0,
                [
                    f"prune-plan TASK-7 session={SID} what=close the run",
                    "prune-plan TASK-7 what=delete the agent dir",
                    f"prune-plan TASK-7 what=prune {clone}'s worktree records and pushed TASK-7-* branches",
                    "prune-plan TASK-7 what=archive",
                ],
            ),
        )
        self.assertNotIn(linear_tracker.M_ARCHIVE, gql.calls)
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))
        self.assertEqual(self.api.order, [])
        PruneDelegate.attach(self.cfg.issue_workdir("TASK-7"))
        PruneDelegate.end(self.cfg.issue_workdir("TASK-7"))
        self.assertEqual(self.prune(gql, dry=True, writable=(self.work,))[0], 0)
        self.assertEqual(self.checkouts("TASK-7"), (wt,))


class RetentionCleanup(PruneScenario):
    def test_writable_dirs_default_to_work_and_the_temp_dirs(self):
        pruner = prune.Pruner(
            None,
            NOW,
            dry=False,
            work=self.work,
            cfg=self.cfg,
            team=TEAM_OBJ,
            roles=ROLES,
        )
        self.assertEqual(pruner.writable, (self.work, *temp_dirs()))

    def test_archives_pm_and_engineer_issues_finished_2h(self):
        gql = gql_for(
            {
                "TASK-1": ("Done", [(30, "In Progress"), (2, "Done")]),
                "TASK-2": ("Canceled", [(50, "Canceled")]),
                "TASK-3": ("Done", [(30, "Done")]),
                "TASK-4": ("Canceled", [(2, "Canceled")]),
            },
            owners={
                "TASK-1": "u-pm",
                "TASK-2": "u-pm",
                "TASK-3": "u-engineer",
                "TASK-4": "u-engineer",
            },
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3", "id-TASK-4"])
        self.assertEqual(
            out.splitlines(),
            [
                "prune-archived TASK-1",
                "prune-archived TASK-2",
                "prune-archived TASK-3",
                "prune-archived TASK-4",
            ],
        )

    def test_archives_canceled_and_duplicate_issues_of_any_assignee(self):
        gql = gql_for(
            {
                "TASK-1": ("Canceled", [(30, "Canceled")]),
                "TASK-2": ("Duplicate", [(30, "Duplicate")]),
                "TASK-3": ("Canceled", [(30, "Canceled")]),
                "TASK-4": ("Duplicate", [(2, "Duplicate")]),
                "TASK-5": ("Duplicate", [(2 - 1 / 3600, "Duplicate")]),
                "TASK-6": ("Canceled", [(30, "Canceled")]),
                "TASK-8": ("Done", [(30, "Done")]),
                "TASK-9": ("Done", [(30, "Done")]),
            },
            owners={
                "TASK-1": None,
                "TASK-2": "u-researcher",
                "TASK-3": "u-other",
                "TASK-4": None,
                "TASK-5": None,
                "TASK-6": "u-researcher",
                "TASK-8": "u-researcher",
                "TASK-9": None,
            },
            children={"TASK-6": {"TASK-7": "started"}},
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3", "id-TASK-4"])
        self.assertEqual(
            out.splitlines(),
            [
                "prune-archived TASK-1",
                "prune-archived TASK-2",
                "prune-archived TASK-3",
                "prune-archived TASK-4",
                "prune-skip TASK-6 reason=archive: sub-issue TASK-7 not finished",
            ],
        )

    def test_linear_finish_stamps_stand_in_for_a_missing_history_move(self):
        for ident in ("TASK-1", "TASK-2", "TASK-3"):
            self.mkc(ident)
        gql = gql_for(
            {
                "TASK-1": ("Duplicate", [(40, "In Progress")]),
                "TASK-2": ("Done", []),
                "TASK-3": ("Duplicate", []),
                "TASK-4": ("Canceled", [(30, "Canceled")]),
            },
            owners={"TASK-2": "u-pm"},
            stamps={
                "TASK-1": {"canceledAt": 30},
                "TASK-2": {"completedAt": 30},
                "TASK-3": {"canceledAt": 1},
                "TASK-4": {"canceledAt": 1},
            },
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2"])
        self.assertEqual(
            out.splitlines(),
            [
                "prune-removed TASK-1 what=agent dir",
                "prune-removed TASK-2 what=agent dir",
                "prune-archived TASK-1",
                "prune-archived TASK-2",
            ],
        )
        self.assertTrue(os.path.isdir(self.cfg.issue_workdir("TASK-3")))
        self.assertIsNotNone(self.checkouts("TASK-3"))

    def test_not_archived(self):
        done = ("Done", [(50, "Done")])
        gql = gql_for(
            {
                "TASK-1": ("Done", [(2 - 1 / 3600, "Done")]),
                "TASK-2": ("Done", [(50, "Done"), (30, "In Progress"), (2 - 1 / 3600, "Done")]),
                "TASK-3": ("Done", []),
                "TASK-4": ("In Progress", [(50, "Done"), (30, "In Progress")]),
                "TASK-5": done,
                "TASK-6": done,
                "TASK-7": done,
            },
            owners={
                "TASK-1": "u-pm",
                "TASK-2": "u-engineer",
                "TASK-3": "u-pm",
                "TASK-4": "u-pm",
                "TASK-5": "u-researcher",
                "TASK-6": "u-other",
                "TASK-7": None,
            },
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(gql.archived, [])
        self.assertEqual(
            out.splitlines(), ["prune-skip TASK-3 reason=archive: finish time unknown"]
        )
        (v,) = gql.finished
        self.assertEqual(sorted(v["a"]), ["u-engineer", "u-pm"])
        self.assertEqual(v["d"], [STATE_IDS["Done"]])
        self.assertEqual(sorted(v["s"]), sorted(FINISHED - {STATE_IDS["Done"]}))

    def test_archive_failure_others_archived(self):
        done = ("Done", [(30, "Done")])
        gql = gql_for(
            {"TASK-1": done, "TASK-2": done, "TASK-3": done},
            owners=dict.fromkeys(("TASK-1", "TASK-2", "TASK-3"), "u-pm"),
            fail={"TASK-1"},
            refuse={"TASK-2"},
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3"])
        self.assertEqual(
            out.splitlines(),
            [
                "prune-error TASK-1 error=archive: LinearError: linear api error: boom",
                "prune-error TASK-2 error=archive: LinearError: issueArchive: success: false",
                "prune-archived TASK-3",
            ],
        )

    def test_archive_reads_every_page_first(self):
        done = ("Done", [(30, "Done")])
        idents = ("TASK-1", "TASK-2", "TASK-3")
        gql = gql_for(
            dict.fromkeys(idents, done), owners=dict.fromkeys(idents, "u-engineer"), page=2
        )
        self.assertEqual(self.prune(gql)[0], 0)
        self.assertEqual(gql.archived, ["id-TASK-1", "id-TASK-2", "id-TASK-3"])
        self.assertEqual([v["c"] for v in gql.finished], [None, "2"])

    def test_an_unfinished_sub_issue_keeps_its_parent(self):
        done = ("Done", [(30, "Done")])
        for dry, archived in (
            (False, "prune-archived TASK-4"),
            (True, "prune-plan TASK-4 what=archive"),
        ):
            with self.subTest(dry=dry):
                gql = gql_for(
                    {"TASK-1": done, "TASK-4": done},
                    owners={"TASK-1": "u-pm", "TASK-4": "u-pm"},
                    children={"TASK-1": {"TASK-2": "completed", "TASK-3": "started"}},
                )
                code, out = self.prune(gql, dry=dry)
                self.assertEqual(code, 0)
                self.assertEqual(gql.archived, [] if dry else ["id-TASK-4"])
                self.assertEqual(
                    out.splitlines(),
                    ["prune-skip TASK-1 reason=archive: sub-issue TASK-3 not finished", archived],
                )

    def test_finished_or_archived_sub_issues_let_the_parent_go(self):
        gql = gql_for(
            {"TASK-1": ("Done", [(30, "Done")])},
            owners={"TASK-1": "u-engineer"},
            children={
                "TASK-1": {"TASK-2": "completed", "TASK-3": "canceled", "TASK-5": "duplicate"}
            },
        )
        self.assertEqual(self.prune(gql), (0, "prune-archived TASK-1"))
        self.assertEqual(gql.archived, ["id-TASK-1"])

    def test_an_open_grandchild_keeps_its_done_grandparent(self):
        gql = gql_for(
            {"TASK-1": ("Done", [(30, "Done")])},
            owners={"TASK-1": "u-pm"},
            children={"TASK-1": {"TASK-2": "completed"}, "TASK-2": {"TASK-3": "started"}},
        )
        self.assertEqual(
            self.prune(gql), (0, "prune-skip TASK-1 reason=archive: sub-issue TASK-3 not finished")
        )
        self.assertEqual((gql.archived, gql.subs), ([], ["id-TASK-2"]))

    def test_candidate_query_failure_keeps_clone_step(self):
        clone = self.mkc("TASK-49")
        issues = gql_for({"TASK-49": ("Done", [(30, "Done")])})

        def gql(query, **v):
            if query == linear_tracker.Q_FINISHED:
                raise LinearError("linear api error: down")
            return issues(query, **v)

        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertIn("prune-error error=archive: Linear: LinearError: linear api error: down", out)
        self.assertFalse(os.path.lexists(clone))

    def test_recorded_run_alone_closes_all_owned_sessions_when_finished(self):
        self.record("TASK-7", SID, [B, A])
        self.record("TASK-8", OTHER_SID, ["other-host-agent"])
        gql = gql_for(
            {"TASK-7": ("Done", [(24, "Done")]), "TASK-8": ("In Progress", [(30, "In Progress")])}
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [f"prune-closed TASK-7 session={SID}", "prune-removed TASK-7 what=agent dir"],
        )
        self.assertEqual(gql.calls.count(linear_tracker.Q_ISSUE), 2)
        self.assertEqual(self.closed(), [B, A])
        again = gql_for({"TASK-7": ("Done", [(24, "Done")]), "TASK-8": ("In Progress", [])})
        self.prune(again)
        self.assertEqual(self.closed(), [B, A])

    def test_unrecorded_matching_names_never_make_candidates(self):
        self.api.live = [A, "agent-pm-engineer-TASK-7", "notes"]
        gql = gql_for({"TASK-7": ("Done", [(30, "Done")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(self.closed(), [])
        self.assertNotIn(linear_tracker.Q_ISSUE, gql.calls)

    def test_only_recorded_configured_roles_make_candidates(self):
        self.record("TASK-7", SID, [A])
        self.record("TASK-9", OTHER_SID, ["designer-agent"], role="designer")
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-9"), ("Done", [(30, "Done")])))
        self.assertEqual(
            self.prune(gql)[1].splitlines(),
            [
                f"prune-closed TASK-7 session={SID}",
                "prune-removed TASK-7 what=agent dir",
                f"prune-skip TASK-9 reason=ConfigError: {self.cfg.issue_workdir('TASK-9')}: "
                "delegate is still alive",
            ],
        )
        self.assertEqual(self.closed(), [A])

    def test_clone_and_session_issue_queried_once_sessions_first(self):
        clone = self.mkc("TASK-7")
        self.record("TASK-7", SID, [A])
        seen, close = [], self.api.close
        gql = gql_for({"TASK-7": ("Canceled", [(30, "Canceled")])})
        with mock.patch.object(
            self.api, "close", lambda name: (seen.append(os.path.isdir(clone)), close(name))
        ):
            code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                f"prune-closed TASK-7 session={SID}",
                "prune-removed TASK-7 what=agent dir",
                "prune-archived TASK-7",
            ],
        )
        self.assertEqual((seen, self.closed()), ([True], [A]))
        self.assertEqual(gql.calls.count(linear_tracker.Q_ISSUE), 1)
        self.assertFalse(os.path.lexists(clone))

    def test_a_failed_close_or_an_unfinished_issue_keeps_the_runs_state(self):
        self.record("TASK-7", SID, [A])
        self.record("TASK-8", OTHER_SID, [B])
        self.api.errors = {A: SessionError("boom")}
        gql = gql_for(
            {"TASK-7": ("Done", [(30, "Done")]), "TASK-8": ("Done", [(2 - 1 / 3600, "Done")])}
        )
        self.assertEqual(self.prune(gql)[0], 3)
        for ident in ("TASK-7", "TASK-8"):
            with self.subTest(ident=ident):
                PruneDelegate.attach(self.cfg.issue_workdir(ident))

    def test_sessions_unknown_prune_no_issue_but_archive(self):
        for dry, archived in (
            (False, "prune-archived TASK-49"),
            (True, "prune-plan TASK-49 what=archive"),
        ):
            with self.subTest(dry=dry):
                self.fresh()
                self.api.order.clear()
                kept = [self.mkc("TASK-49"), self.mkc("TASK-49", "p", "q")]
                self.record("TASK-49", SID, [A])
                self.unknown = True
                gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
                code, out = self.prune(gql, dry=dry)
                self.assertEqual(
                    (code, out.splitlines()),
                    (3, ["prune-error error=run inventory unknown", archived]),
                )
                self.assertTrue(all(os.path.isdir(os.path.join(p, ".git")) for p in kept))
                self.assertNotIn(linear_tracker.Q_ISSUE, gql.calls)
                self.assertEqual(gql.archived, [] if dry else ["id-TASK-49"])
                self.assertEqual(self.api.order, [])

    def test_close_failure_counted(self):
        for error in (SessionError("boom"), ConfigError("bad name")):
            with self.subTest(error=type(error).__name__):
                self.api.order.clear()
                self.record("TASK-7", SID, [B, A])
                self.api.errors = {B: error}
                code, out = self.prune(gql_for({"TASK-7": ("Done", [(30, "Done")])}))
                self.assertEqual(code, 3)
                self.assertEqual(
                    out.splitlines(),
                    [f"prune-error TASK-7 session={SID} error={type(error).__name__}: {error}"],
                )
                self.assertEqual(self.closed(), [B, A])
                self.assertEqual(self.checkouts("TASK-7"), ())

    def test_a_failed_close_keeps_the_agent_dir_until_a_later_tick_closes_the_run(self):
        clone = self.mkc("TASK-7")
        self.record("TASK-7", SID, [B, A])
        self.api.errors = {B: SessionError("boom")}
        gql = gql_for({"TASK-7": ("Done", [(30, "Done")])})
        code, out = self.prune(gql)
        self.assertEqual(
            (code, out.splitlines(), self.closed()),
            (3, [f"prune-error TASK-7 session={SID} error=SessionError: boom"], [B, A]),
        )
        self.assertTrue(os.path.isdir(clone))
        self.api.errors = {}
        self.api.order.clear()
        code, out = self.prune(gql)
        self.assertEqual(
            (code, out.splitlines(), self.closed()),
            (
                0,
                [f"prune-closed TASK-7 session={SID}", "prune-removed TASK-7 what=agent dir"],
                [B, A],
            ),
        )
        self.assertIsNone(self.checkouts("TASK-7"))

    def test_failed_run_close_does_not_stop_another_finished_issue(self):
        self.record("TASK-7", SID, [A])
        self.record("TASK-8", OTHER_SID, ["other-host-agent"])
        self.api.errors = {A: SessionError("boom")}
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(30, "Done")])))
        code, out = self.prune(gql)
        self.assertEqual(code, 3)
        self.assertEqual(self.closed(), [A, "other-host-agent"])
        self.assertEqual(
            out.splitlines(),
            [
                f"prune-error TASK-7 session={SID} error=SessionError: boom",
                f"prune-closed TASK-8 session={OTHER_SID}",
                "prune-removed TASK-8 what=agent dir",
            ],
        )

    def test_a_run_without_delegate_state_skips_once_and_other_issues_still_prune(self):
        clone = self.mkc("TASK-7")
        self.record("TASK-7", SID, [A])
        self.record("TASK-8", OTHER_SID, ["other-host-agent"])
        PruneDelegate.lose(self.cfg.issue_workdir("TASK-7"))
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(30, "Done")])))
        skip = (
            f"prune-skip TASK-7 sid={SID} reason=ConfigError: "
            f"{self.cfg.issue_workdir('TASK-7')!r}: no delegate.json"
        )
        code, out = self.prune(gql)
        self.assertEqual(code, 0)
        self.assertEqual(self.closed(), ["other-host-agent"])
        self.assertFalse(os.path.exists(clone))
        self.assertIn(skip, out.splitlines())
        self.assertIn(f"prune-closed TASK-8 session={OTHER_SID}", out.splitlines())
        self.assertNotIn(skip, self.prune(gql)[1].splitlines())

    def test_promote_tick_archives_with_its_roles(self):
        gql = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        code, _, out = self.tick(gql)
        self.assertEqual((code, gql.archived), (0, ["id-TASK-1"]))
        self.assertIn("prune-archived TASK-1", out)
        self.assertEqual(self.user_queries, len(tp.ROLE))

    def test_promote_tick_closes_recorded_runs_through_the_delegate(self):
        self.record("TASK-7", SID, [A])
        code, _, out = self.tick(gql_for({"TASK-7": ("Done", [(30, "Done")])}))
        self.assertEqual(code, 0)
        self.assertIn(f"prune-closed TASK-7 session={SID}", out)
        self.assertEqual(self.api.order, [("close", A)])

    def test_prune_failure_leaves_handoff_alone(self):
        clone = self.mkc("TASK-49")

        def down(_query, **_v):
            raise LinearError("linear api error: down")

        code, state, out = self.tick(down)
        self.assertEqual((code, state), (0, "Done"))
        self.assertIn("prune-error TASK-49 error=Linear: LinearError: linear api error: down", out)
        self.assertTrue(os.path.isdir(clone))

    def test_an_outage_reading_an_issue_stops_prune(self):
        clones = [self.mkc(ident) for ident in ("TASK-48", "TASK-49")]
        issues = gql_for(dict.fromkeys(("TASK-48", "TASK-49"), ("Done", [(30, "Done")])))
        self.outage(
            "issue", issues, [(linear_tracker.Q_ISSUE, {"service": HARNESS, "i": "TASK-48"})]
        )
        self.assertEqual(issues.calls, [])
        self.assertTrue(all(os.path.isdir(c) for c in clones))

    def test_an_outage_reading_the_finished_issues_stops_prune(self):
        issues = gql_for({"TASK-1": ("Done", [(30, "Done")])}, owners={"TASK-1": "u-pm"})
        self.outage("issues", issues, [(linear_tracker.Q_FINISHED, mock.ANY)])

    def test_an_outage_archiving_stops_prune(self):
        done = ("Done", [(30, "Done")])
        issues = gql_for(
            {"TASK-1": done, "TASK-2": done}, owners={"TASK-1": "u-pm", "TASK-2": "u-pm"}
        )
        self.outage(
            "issueArchive",
            issues,
            [(linear_tracker.M_ARCHIVE, {"service": HARNESS, "i": "id-TASK-1"})],
        )


class WorkdirRetention(PruneScenario):
    """FR-5: a workdir without agent files goes whole 7 days after its issue finished."""

    def bare(self, ident, sid=SID, *, live=False):
        workdir = self.cfg.issue_workdir(ident)
        self.record(ident, sid, [A], agent=False)
        if not live:
            PruneDelegate.end(workdir)
        write(os.path.join(workdir, "writeback.json"), "{}")
        write(os.path.join(workdir, "writeback.lock"), "")
        self.assertEqual(sorted(os.listdir(workdir)), ["run", "writeback.json", "writeback.lock"])
        return workdir

    def test_a_bare_workdir_finished_7_days_is_removed(self):
        self.bare("TASK-7")
        self.bare("TASK-8", OTHER_SID)
        gql = gql_for(
            {
                "TASK-7": ("Done", [(8 * 24, "Done")]),
                "TASK-8": ("Canceled", [(7 * 24, "Canceled")]),
            }
        )
        self.assertEqual(
            self.prune(gql),
            (
                0,
                "prune-removed TASK-7 what=workdir\nprune-removed TASK-8 what=workdir\n"
                "prune-archived TASK-8",
            ),
        )
        self.assertEqual(os.listdir(self.work), [])

    def test_a_bare_workdir_with_a_read_only_go_module_cache_is_removed(self):
        module = os.path.join(self.bare("TASK-7"), "gopath", "mod", "x@v1.0.12")
        os.makedirs(module)
        write(os.path.join(module, "go.mod"), "module x\n")
        os.chmod(os.path.join(module, "go.mod"), 0o444)
        for path in (module, os.path.dirname(module)):
            os.chmod(path, 0o555)
        gql = gql_for({"TASK-7": ("Done", [(8 * 24, "Done")])})
        self.assertEqual(self.prune(gql), (0, "prune-removed TASK-7 what=workdir"))
        self.assertEqual(os.listdir(self.work), [])

    def test_a_bare_workdir_finished_3_days_is_kept(self):
        workdir = self.bare("TASK-7")
        files = tree(workdir)
        for hours in (3 * 24, 7 * 24 - 1 / 3600):
            with self.subTest(hours=hours):
                self.assertEqual(
                    self.prune(gql_for({"TASK-7": ("Done", [(hours, "Done")])})), (0, "")
                )
                self.assertEqual(tree(workdir), files)

    def test_agent_files_take_the_2_hour_step_first(self):
        self.record("TASK-7", SID, [A])
        workdir = self.cfg.issue_workdir("TASK-7")
        gql = gql_for({"TASK-7": ("Done", [(8 * 24, "Done")])})
        self.assertEqual(
            self.prune(gql),
            (0, f"prune-closed TASK-7 session={SID}\nprune-removed TASK-7 what=agent dir"),
        )
        self.assertEqual(os.listdir(workdir), ["run"])
        self.assertEqual(self.prune(gql), (0, "prune-removed TASK-7 what=workdir"))
        self.assertFalse(os.path.lexists(workdir))

    def test_a_live_run_skips_once_and_keeps_the_workdir(self):
        workdir = self.bare("TASK-7", live=True)
        files = tree(workdir)
        gql = gql_for({"TASK-7": ("Done", [(8 * 24, "Done")])})
        self.assertEqual(
            self.prune(gql),
            (0, f"prune-skip TASK-7 reason=ConfigError: {workdir}: delegate is still alive"),
        )
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(tree(workdir), files)

    def test_a_failed_removal_is_an_error_and_others_go_on(self):
        for ident, sid in (("TASK-7", SID), ("TASK-8", OTHER_SID), ("TASK-9", SID)):
            self.bare(ident, sid)
        real = shutil.rmtree

        def clean(workdir, **_kwargs):
            if workdir.endswith("TASK-7"):
                raise SessionError("host gone")

        def rmtree(path, **kwargs):
            if path.endswith("TASK-8"):
                raise PermissionError("denied")
            real(path, **kwargs)

        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8", "TASK-9"), ("Done", [(8 * 24, "Done")])))
        with (
            mock.patch.object(PruneDelegate, "clean", side_effect=clean),
            mock.patch.object(shutil, "rmtree", side_effect=rmtree),
        ):
            code, out = self.prune(gql)
        self.assertEqual(
            (code, out.splitlines()),
            (
                3,
                [
                    "prune-error TASK-7 error=remove: SessionError: host gone",
                    "prune-error TASK-8 error=remove: PermissionError: denied",
                    "prune-removed TASK-9 what=workdir",
                ],
            ),
        )
        self.assertEqual(sorted(os.listdir(self.work)), ["TASK-7", "TASK-8"])

    def test_dry_run_plans_the_removal(self):
        workdir = self.bare("TASK-7")
        files = tree(self.work)
        gql = gql_for({"TASK-7": ("Done", [(8 * 24, "Done")])})
        self.assertEqual(
            self.prune(gql, dry=True), (0, "prune-plan TASK-7 what=delete the workdir")
        )
        self.assertEqual(tree(self.work), files)
        with mock.patch.object(PruneDelegate, "clean") as clean:
            self.prune(gql, dry=True)
        self.assertEqual(clean.call_args, mock.call(workdir, dry=True))

    def test_an_unconfigured_kind_is_not_queried(self):
        workdir = self.bare("TASK-7")
        with open(self.cfg.runs_log) as f:
            rows = f.read().replace('"delegate_kind": "harness"', '"delegate_kind": "recorded"')
        write(self.cfg.runs_log, rows)
        files = tree(workdir)
        gql = gql_for({"TASK-7": ("Done", [(8 * 24, "Done")])})
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertNotIn(linear_tracker.Q_ISSUE, gql.calls)
        self.assertEqual(tree(workdir), files)

    def test_unfinished_or_unknown_finish_keeps_the_workdir(self):
        self.bare("TASK-7")
        self.bare("TASK-8", OTHER_SID)
        files = tree(self.work)
        gql = gql_for(
            {
                "TASK-7": ("In Progress", [(9 * 24, "Done"), (8 * 24, "In Progress")]),
                "TASK-8": ("Done", []),
            }
        )
        self.assertEqual(self.prune(gql), (0, "prune-skip TASK-8 reason=finish time unknown"))
        self.assertEqual(tree(self.work), files)

    def test_only_real_directories_are_visited(self):
        os.symlink(self.outside, os.path.join(self.work, "TASK-7"))
        write(os.path.join(self.work, "TASK-8"), "x")
        gql = gql_for(dict.fromkeys(("TASK-7", "TASK-8"), ("Done", [(8 * 24, "Done")])))
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertNotIn(linear_tracker.Q_ISSUE, gql.calls)
        self.assertOutsideKept()
        self.assertTrue(os.path.isfile(os.path.join(self.work, "TASK-8")))


class Stalled(PruneScenario):
    """A stalled issue's checkouts and temp files go STALL after its latest move or run."""

    WEEK = 7 * 24

    def stalled(self, ident="TASK-7", *, hours=8 * 24, run=None, state="In Review"):
        """A W with agent files of an ended run, its issue moved to `state` `hours` ago."""
        workdir = self.cfg.issue_workdir(ident)
        self.record(ident, SID, [A], hours=hours if run is None else run)
        PruneDelegate.end(workdir)
        agent = os.path.join(workdir, "agent")
        for path in ("tmp/msg", "publish/Research/r.md", "prd.md"):
            os.makedirs(os.path.dirname(os.path.join(agent, path)), exist_ok=True)
            write(os.path.join(agent, path), "x")
        write(os.path.join(workdir, "writeback.json"), "{}")
        return workdir, gql_for({ident: (state, [(hours + 1, "In Progress"), (hours, state)])})

    def test_stalled_week_drops_scratch_keeps_logs(self):
        clone = self.fake_clone()
        workdir, gql = self.stalled(state="Handoff")
        wt = self.mkg("TASK-7")
        calls = []

        def git(argv, _timeout):
            calls.append(argv)
            return CompletedProcess(argv, 0, "", "")

        run = tree(os.path.join(workdir, "run"))
        self.assertEqual(
            self.prune(gql, git=git, writable=(self.work,)),
            (0, "prune-removed TASK-7 what=scratch"),
        )
        self.assertFalse(os.path.lexists(wt))
        self.assertEqual(sorted(os.listdir(workdir)), ["agent", "run", "writeback.json"])
        self.assertEqual(os.listdir(os.path.join(workdir, "agent")), ["prd.md"])
        self.assertEqual(tree(os.path.join(workdir, "run")), run)
        self.assertEqual(
            [argv[5:] for argv in calls],
            [["-C", clone, f"--git-dir={clone}/.git", "worktree", "prune"]],
        )
        self.assertEqual(self.api.order, [])

    def test_recent_or_active_or_unknown_kept(self):
        issues = {}
        for n, (hours, run, state) in enumerate(
            (
                (self.WEEK - 1 / 3600, None, "In Review"),
                (9 * 24, self.WEEK - 1, "In Review"),
                (9 * 24, None, "Todo"),
                (9 * 24, None, "In Progress"),
            )
        ):
            issues.update(
                self.stalled(f"TASK-{n + 10}", hours=hours, run=run, state=state)[1].issues
            )
        self.stalled("TASK-20")
        issues["TASK-20"] = ("In Review", [(9 * 24, "In Progress")])
        files = tree(self.work)
        gql = gql_for(issues)
        self.assertEqual(self.prune(gql), (0, "prune-skip TASK-20 reason=stall time unknown"))
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(tree(self.work), files)

    def test_live_refused_and_dry_run_plans(self):
        workdir, gql = self.stalled()
        files = tree(workdir)
        self.assertEqual(
            self.prune(gql, dry=True),
            (0, "prune-plan TASK-7 what=delete the checkouts and temp files"),
        )
        self.assertEqual(tree(workdir), files)
        PruneDelegate.attach(workdir).detach()
        files = tree(workdir)
        self.assertEqual(
            self.prune(gql),
            (0, f"prune-skip TASK-7 reason=ConfigError: {workdir}: delegate is still alive"),
        )
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(tree(workdir), files)
        self.assertEqual(self.api.order, [])

    def test_no_event_once_scratch_gone(self):
        workdir, gql = self.stalled()
        self.assertEqual(self.prune(gql), (0, "prune-removed TASK-7 what=scratch"))
        self.assertEqual(self.prune(gql), (0, ""))
        self.assertEqual(os.listdir(os.path.join(workdir, "agent")), ["prd.md"])

    def test_a_duplicate_issue_is_finished_not_stalled(self):
        _, gql = self.stalled(state="Duplicate")
        self.assertEqual(
            self.prune(gql),
            (
                0,
                f"prune-closed TASK-7 session={SID}\nprune-removed TASK-7 what=agent dir\n"
                "prune-archived TASK-7",
            ),
        )
        self.assertIsNone(self.checkouts("TASK-7"))


class PruneCommand(PruneScenario):
    """agent-pm-orchestrator prune: one prune tick alone, as promote's tick runs it."""

    def command(self, prune_gql, *argv):
        write(
            os.path.join(self.agent_pm, "config.local.toml"),
            f'[orchestrator]\nwork_dir = "{self.root}"\n' + CORE_LOCAL,
        )
        path = os.path.join(self.root, "config.toml")
        write(path, tp.CONFIG)
        fake = tp.FakeLinear()

        def gql(query, **v):
            board = query not in (
                linear_tracker.Q_ISSUE,
                linear_tracker.Q_FINISHED,
                linear_tracker.M_ARCHIVE,
            )
            return (fake if board else prune_gql)(query, **v)

        before, out, err = len(logged(self.logs)), io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = prune.main(list(argv), gql=gql, now=NOW, config_path=path, pruner=self.pruner)
        lines = err.getvalue().splitlines()
        copies = [json.loads(line) for line in lines if line.startswith("{")]
        said = "\n".join(show(e) for e in logged(self.logs)[before:] + copies)
        rest = "".join(f"{line}\n" for line in lines if not line.startswith("{"))
        return code, out.getvalue(), rest, said

    def test_a_local_board_is_refused_after_config(self):
        os.environ[config.BOARD_ENV] = os.path.join(self.home, "board")
        write(os.path.join(self.agent_pm, "config.local.toml"), "[orchestrator]\n" + CORE_LOCAL)
        path = os.path.join(self.root, "config.toml")
        write(path, tp.CONFIG.replace('tracker = "linear"', 'tracker = "local"'))
        fake, out, err = tp.FakeLinear(), io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = prune.main([], gql=fake, now=NOW, config_path=path)
        self.assertEqual(
            (code, out.getvalue(), err.getvalue(), fake.calls),
            (2, "", 'prune.py: prune needs [orchestrator] tracker = "linear"\n', []),
        )

    def test_a_tick_deletes_finished_checkouts_and_archives(self):
        clone = self.mkc("TASK-49")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
        self.assertEqual(self.command(gql)[:3], (0, "", ""))
        self.assertFalse(os.path.exists(clone))
        self.assertEqual(gql.archived, ["id-TASK-49"])
        self.assertOutsideKept()

    def test_a_duplicate_issue_is_finished_by_its_state_type(self):
        clone = self.mkc("TASK-49")
        gql = gql_for(
            {"TASK-49": ("Duplicate", [(30, "In Progress"), (2, "Duplicate")])},
            owners={"TASK-49": "u-pm"},
        )
        self.assertEqual(self.command(gql)[:3], (0, "", ""))
        self.assertFalse(os.path.exists(clone))
        self.assertEqual(gql.archived, ["id-TASK-49"])

    def test_a_dry_run_plans_and_changes_nothing(self):
        clone = self.mkc("TASK-49")
        gql = gql_for({"TASK-49": ("Done", [(30, "Done")])}, owners={"TASK-49": "u-pm"})
        code, out, err, said = self.command(gql, "--dry-run")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertTrue(os.path.isdir(os.path.join(clone, ".git")))
        self.assertEqual(gql.archived, [])
        self.assertEqual(
            said.splitlines(),
            [
                "prune-plan TASK-49 what=delete the agent dir",
                "prune-plan TASK-49 what=archive",
            ],
        )

    def test_a_failed_step_exits_1_after_the_rest(self):
        def down(query, **v):
            if query == linear_tracker.Q_ISSUE:
                raise LinearError("linear api error: down")
            return gql_for({})(query, **v)

        self.mkc("TASK-49")
        code, _, _, said = self.command(down)
        self.assertEqual(code, 1)
        self.assertIn("prune-error TASK-49 error=Linear: LinearError: linear api error: down", said)

    def test_other_arguments_are_a_usage_error(self):
        for argv in (["--now"], ["TASK-1"], ["--dry-run", "x"]):
            with self.subTest(argv=argv):
                code, out, err, said = self.command(gql_for({}), *argv)
                self.assertEqual((code, out, said), (2, "", ""))
                self.assertIn("agent-pm-orchestrator prune", err)
