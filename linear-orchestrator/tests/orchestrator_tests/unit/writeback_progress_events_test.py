import json
import os
import shutil
import tempfile
from dataclasses import replace
from datetime import datetime
from unittest import mock

from agent_pm.delegate import Begun, Finished, Progress, Result
from agent_pm.linear_orchestrator.engine import config, runs
from agent_pm.linear_orchestrator.engine.types import LinearError, Stopped
from agent_pm.linear_orchestrator.linear import issues, writeback
from orchestrator_tests.case import Case
from orchestrator_tests.unit import board_ids, run_fixtures
from orchestrator_tests.unit.clock import logs_at
from orchestrator_tests.unit.writeback_data import (
    HANDOFF,
    HUMANS,
    ID,
    OTHER_SID,
    PROJECT,
    SID,
    SRC_UUID,
    START,
    STATES,
    TARGET,
    UUID,
    comment,
    issue,
    move,
    outcome,
    post,
    reread,
    sha,
    sub,
)
from orchestrator_tests.unit.writeback_fakes import Gql
from orchestrator_tests.unit.writeback_scenarios import Base
from orchestrator_tests.unit.writeback_support import PR


class ProgressEvents(Base):
    def test_start_once_per_sid(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        start = Progress("start", "first build of the PRD")
        writeback.progress(ctx, start)
        writeback.progress(ctx, start)
        self.assertEqual(gql.calls, [post("start", "Build started: first build of the PRD")])
        self.assertEqual(self.ledger(ctx), {SID: ["start"]})
        self.assertEqual(self.lines(), [f"step {ID} step=start"])
        writeback.progress(replace(ctx, sid=OTHER_SID), start)
        self.assertEqual(
            gql.calls[1:], [post("start", "Build started: first build of the PRD", OTHER_SID)]
        )

    def test_start_comment_is_the_roles_lead_and_the_runs_pick(self):
        pick = "light-build: a template wording tweak, like TASK-226."
        for role, text, body in (
            ("researcher", "budget 2 rounds", "Research started: budget 2 rounds"),
            ("pm", "budget 2 rounds", "PRD started: budget 2 rounds"),
            ("engineer", pick, f"Build started: {pick}"),
        ):
            with self.subTest(role=role, text=text):
                gql = Gql()
                writeback.progress(self.ctx(role, gql=gql), Progress("start", text))
                self.assertEqual(gql.calls, [post("start", body)])
                if role == "engineer":
                    self.assertTrue(issues.BUILD_STARTED.match(body))

    def test_ignores_begun_and_finished(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        writeback.progress(ctx, Begun(SID))
        writeback.progress(ctx, Finished(Result(0, outcome("done", summary="x"))))
        self.assertEqual(gql.calls, [])
        self.assertEqual(self.lines(), [])

    def test_posts_every_other_mark_once_per_text(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        for text in ("12 passing", "12 passing", "13 passing"):
            writeback.progress(ctx, Progress("tests", text))
        steps = [f"progress:tests:{sha('12 passing')}", f"progress:tests:{sha('13 passing')}"]
        self.assertEqual(
            gql.calls,
            [
                post(steps[0], "Progress (tests): 12 passing"),
                post(steps[1], "Progress (tests): 13 passing"),
            ],
        )
        self.assertEqual(self.ledger(ctx), {SID: steps})

    def test_swallows_system_exit_and_retries_later(self):
        gql = Gql(fail=lambda name, v: LinearError("linear api error: down"))
        ctx = self.ctx(gql=gql)
        writeback.progress(ctx, START)
        self.assertEqual(
            self.lines(), [f"step-error {ID} step=start error=LinearError: linear api error: down"]
        )
        ok = Gql()
        writeback.progress(replace(ctx, gql=ok), START)
        self.assertEqual(ok.calls, [post("start", "Build started: x")])

    def test_signal_exit_passes_through(self):
        gql = Gql(fail=lambda name, v: Stopped(129))
        ctx = self.ctx(gql=gql)
        with self.assertRaises(Stopped) as cm:
            writeback.progress(ctx, START)
        self.assertEqual(cm.exception.code, 129)
        self.no_ledger(ctx)
        self.assertEqual(self.lines(), [])

    def test_never_raises(self):
        gql = Gql(fail=lambda name, v: RuntimeError("boom"))
        open(os.path.join(self.tmp, "file"), "w").close()
        with logs_at(os.path.join(self.tmp, "file", "logs")):
            writeback.progress(self.ctx(gql=gql), START)
        writeback.progress(replace(self.ctx(), role="nope"), START)
        self.assertEqual(self.lines(), [f"step-error {ID} step=sink error=KeyError: 'nope'"])


class FinishNeverRaises(Base):
    def test_unknown_role_logged(self):
        self.assertFalse(writeback.finish(self.ctx("nope"), outcome("done", summary="x")))
        self.assertEqual(self.lines(), [f"step-error {ID} step=finish error=KeyError: 'nope'"])

    def test_signal_exit_stops_the_steps(self):
        for name in ("read", "subscribe", "comment", "attach"):
            with self.subTest(name=name):
                gql = Gql(fail=lambda n, v: Stopped(143) if n == name else None)
                ctx = self.ctx(gql=gql)
                self.assertFalse(
                    writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR))
                )
                self.assertEqual(gql.calls[-1][0], name)
                self.assertEqual(
                    self.lines()[-1], f"step-error {ID} step=finish error=Stopped: 143"
                )
                self.assertNotIn("state", [c[0] for c in gql.calls])


class Bounce(Base):
    DONE = (STATES["done"], STATES["in_progress"])

    def src(self, team=board_ids.TEAM):
        return {"id": SRC_UUID, "team": {"id": team}}

    def text(self, reason, tail=""):
        return (
            f"Repo check failed: {reason}. To build it, move PRD-3 to Handoff again with a comment "
            f"`Repo: <owner>/<name>` naming the target repo.{tail}"
        )

    def question(self, reason):
        return (
            "Question: repo check failed: "
            + reason
            + ". Fix the description's `Repo:` line, then move this issue back to Todo."
        )

    def test_handoff_path(self):
        gql = Gql(issue=self.src(), states=self.DONE)
        reason = "ophis/x: no push permission"
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), reason)
        t = self.text(reason)
        self.assertEqual(
            gql.calls,
            [
                ("id", {"i": "PRD-3"}),
                ("unarchive", {"i": SRC_UUID}),
                sub(HUMANS[0], SRC_UUID),
                sub(HUMANS[1], SRC_UUID),
                comment(t, SRC_UUID),
                reread(SRC_UUID),
                move("in_review", SRC_UUID),
                comment(t),
                reread(),
                move("canceled"),
            ],
        )
        self.assertEqual(self.lines(), [])

    def test_skipped_moves_logged(self):
        gql = Gql(issue=self.src(), states=(STATES["handoff"], STATES["todo"]))
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
        t = self.text("r")
        self.assertEqual(
            gql.calls[-4:], [comment(t, SRC_UUID), reread(SRC_UUID), comment(t), reread()]
        )
        self.assertEqual(
            self.lines(),
            [
                f"skip PRD-3 reason=move to in_review: issue is {STATES['handoff']}",
                f"skip {ID} reason=move to canceled: issue is {STATES['todo']}",
            ],
        )

    def test_mapped_reason_names_project_repos(self):
        gql = Gql(issue=self.src())
        reason = "project mapping ophis/x: not found or no access (HTTP 404)"
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), reason)
        self.assertIn(
            comment(
                self.text(
                    reason,
                    " Or fix ~/.agent-pm/config.local.toml's [project_repos] entry.",
                )
            ),
            gql.calls,
        )

    def test_unarchive_failure_ignored(self):
        for err in (LinearError("linear api error: not archived"), False):
            with self.subTest(err=err):
                gql = Gql(
                    issue=self.src(),
                    states=self.DONE,
                    fail=lambda name, v: err if name == "unarchive" else None,
                )
                writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
                self.assertEqual(gql.calls[-1], move("canceled"))

    def test_subscribe_failure_noted_move_made(self):
        gql = Gql(
            issue=self.src(),
            states=self.DONE,
            fail=lambda name, v: (
                LinearError("linear api error: boom")
                if name == "subscribe" and v["e"] == HUMANS[1]
                else None
            ),
        )
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
        t = self.text("r")
        self.assertIn(
            comment(
                t + "\n\nCould not subscribe bob@example.com: LinearError: linear api error: boom",
                SRC_UUID,
            ),
            gql.calls,
        )
        self.assertEqual(
            gql.calls[-5:],
            [reread(SRC_UUID), move("in_review", SRC_UUID), comment(t), reread(), move("canceled")],
        )

    def test_question_path(self):
        reason = "unreadable Repo line: 'nope'"
        for name, description, src, lookup in (
            ("not a handoff", "Repo: nope\n\nBuild it.", None, []),
            (
                "source in another team",
                HANDOFF,
                self.src(team="00000000-0000-4000-8000-0000000000ff"),
                [("id", {"i": "PRD-3"})],
            ),
            ("source missing", HANDOFF, None, [("id", {"i": "PRD-3"})]),
        ):
            with self.subTest(name):
                gql = Gql(issue=src)
                writeback.bounce(self.ctx(gql=gql), issue(description), reason)
                self.assertEqual(
                    gql.calls,
                    [
                        *lookup,
                        sub(HUMANS[0]),
                        sub(HUMANS[1]),
                        comment(self.question(reason)),
                        reread(),
                        move("in_review"),
                    ],
                )

    def test_question_subscribe_failure_noted(self):
        gql = Gql(
            fail=lambda name, v: False if name == "subscribe" and v["e"] == HUMANS[0] else None
        )
        writeback.bounce(self.ctx(gql=gql), issue("x"), "r")
        self.assertEqual(
            gql.calls[-3:],
            [
                comment(
                    self.question("r") + "\n\nCould not subscribe ann@example.com: "
                    "LinearError: issueSubscribe: success: false"
                ),
                reread(),
                move("in_review"),
            ],
        )

    def test_signal_exit_raises(self):
        for name in ("unarchive", "subscribe"):
            with self.subTest(name=name):
                gql = Gql(
                    issue=self.src(), fail=lambda n, v: Stopped(129) if n == name else None
                )
                with self.assertRaises(Stopped):
                    writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
                self.assertEqual(gql.calls[-1][0], name)

    def test_other_failures_raise(self):
        gql = Gql(issue=self.src(), fail=lambda name, v: False if name == "comment" else None)
        with self.assertRaises(LinearError):
            writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), "r")
        self.assertEqual(gql.calls[-1], comment(self.text("r"), SRC_UUID))
        gql = Gql(
            fail=lambda name, v: LinearError("linear api error: x") if name == "state" else None
        )
        with self.assertRaises(LinearError):
            writeback.bounce(self.ctx(gql=gql), issue("x"), "r")


class ContextBuilder(run_fixtures.Base):
    def test_the_one_context_of_a_role_from_the_config(self):
        cfg = config.load()
        gql = mock.Mock(return_value={})
        for resume, target in ((False, TARGET), (True, None)):
            with self.subTest(resume=resume):
                ctx = writeback.context(
                    cfg,
                    "engineer",
                    gql,
                    ident=ID,
                    issue_id=UUID,
                    sid=SID,
                    resume=resume,
                    project=PROJECT,
                    target=target,
                )
                want = writeback.Context(
                    ident=ID,
                    issue_id=UUID,
                    role="engineer",
                    sid=SID,
                    resume=resume,
                    project=PROJECT,
                    workdir=os.path.join(self.work, "work", ID),
                    gql=ctx.gql,
                    humans=("me@x.com",),
                    states=cfg.states,
                    repos={PROJECT: "ophis/agent-pm"},
                    team=cfg.team,
                    target=target,
                )
                self.assertEqual(ctx, want)
                ctx.gql("q", i=UUID)
                gql.assert_called_with("q", service=run_fixtures.KEY, i=UUID)


class RunsNote(Case):
    def test_one_durable_json_line_per_note(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "logs", "runs.jsonl")
        at = "2026-10-11T18:00:00+08:00"
        with mock.patch.object(os, "fsync", wraps=os.fsync) as spy:
            runs.note(path, "limit", issue=ID, sid=SID, runtime="claude", resets_at=None)
            runs.note(path, "limit", issue=ID, sid=OTHER_SID, runtime="claude", resets_at=at)
        self.assertEqual(spy.call_count, 4)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.endswith("\n"))
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(
            [list(r) for r in rows], [["ts", "kind", "issue", "sid", "runtime", "resets_at"]] * 2
        )
        for r in rows:
            ts = datetime.fromisoformat(r["ts"])
            self.assertIsNotNone(ts.utcoffset())
            self.assertEqual(r["ts"], ts.isoformat(timespec="seconds"))
        self.assertEqual(
            [{k: v for k, v in r.items() if k != "ts"} for r in rows],
            [
                {"kind": "limit", "issue": ID, "sid": SID, "runtime": "claude", "resets_at": None},
                {
                    "kind": "limit",
                    "issue": ID,
                    "sid": OTHER_SID,
                    "runtime": "claude",
                    "resets_at": at,
                },
            ],
        )

