"""Shared writeback output expectations."""

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from unittest import mock

from agent_pm.delegate import STATUSES, Begun, Finished, Outcome, Progress, Result
from agent_pm.linear_orchestrator.engine import (
    config,
    issues,
    log,
    ops,
    runs,
    writeback,
)
from agent_pm.linear_orchestrator.engine.types import Invalid, Issue, LinearError, Stopped
from orchestrator_tests.case import Case
from orchestrator_tests.unit import board_ids, run_fixtures
from orchestrator_tests.unit.clock import logs_at
from orchestrator_tests.unit.run_fixtures import logged, show
from orchestrator_tests.unit.writeback_fixtures import APPROVE_MAPPED, DOC, PR, PRD

ID, UUID = "ENG-7", "11111111-2222-4333-8444-555555555555"

SID = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"

OTHER_SID = "9c1d2e3f-4a5b-4c6d-8e7f-a0b1c2d3e4f5"

SRC_UUID = "99999999-8888-4777-8666-555555555555"

OTHER_ISSUE = "77777777-6666-4555-8444-333333333333"

HUMANS = ("ann@example.com", "bob@example.com")

STATES = board_ids.STATES

PROJECT = "121166b1-191a-4461-bec4-42f1c2dc0ddd"

REPOS = {PROJECT: "ophis/agent-pm"}

TARGET = ("ophis", "agent-pm")

TREE = "https://github.com/ophis/agent-pm/tree/ENG-7-session-registry"

SPEC_TEXT = "# Spec\n\nBuild the registry.\n"

PLAN_TEXT = "# Plan\n\nRESUME: phase=2\n\n- [ ] task 1\n"

FOOTER = "Answer in a comment, then move this issue back to Todo."

APPROVE = "**🔴 To approve, move this issue to Handoff with a comment `Repo: <owner>/<name>` naming the target repo.**"

NOT_FOUND = LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")

START = Progress("start", "x")

T = datetime(2026, 10, 11, 10, 0, tzinfo=UTC).timestamp()

NAMES = {
    ops.M_SUBSCRIBE: "subscribe",
    ops.M_COMMENT: "comment",
    ops.M_COMMENT_ID: "comment",
    ops.Q_COMMENT: "find",
    ops.M_STATE: "state",
    writeback.M_TITLE: "title",
    writeback.M_ATTACH: "attach",
    writeback.M_UNARCHIVE: "unarchive",
    writeback.Q_ATTACHED: "read",
    ops.Q_ISSUE_STATE: "reread",
    writeback.Q_ID: "id",
}

FIELDS = {
    "subscribe": "issueSubscribe",
    "comment": "commentCreate",
    "state": "issueUpdate",
    "title": "issueUpdate",
    "attach": "attachmentLinkURL",
    "unarchive": "issueUnarchive",
}


def mine(body, issue=UUID):
    return {"body": body, "issue": issue, "mine": True}


READ = ("read", {"i": UUID})


def reread(i=UUID):
    return ("reread", {"i": i})


def sub(e, i=UUID):
    return ("subscribe", {"i": i, "e": e})


def cid(step, sid=SID):
    return ops.client_id(sid, step)


def post(step, b, sid=SID):
    return ("comment", {"c": cid(step, sid), "i": UUID, "b": b})


def find(step):
    return ("find", {"c": cid(step)})


def comment(b, i=UUID):
    return ("comment", {"i": i, "b": b})


def move(s, i=UUID):
    return ("state", {"i": i, "s": STATES[s]})


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def spec_comment(name="ENG-7-spec.md", text=SPEC_TEXT):
    return post(f"file:{name}:{sha(text)}", f"**Spec** `{name}`\n\n---\n\n{text}")


def plan_comment(name="ENG-7-plan.md", text=PLAN_TEXT):
    return post(f"file:{name}:{sha(text)}", f"**Plan** `{name}`\n\n---\n\n{text}")


def expect(
    *,
    body,
    state,
    files=(),
    title=None,
    subscribe=True,
    attach=None,
    attach_title=None,
    resume=False,
):

    def posted(call):
        return [("find", {"c": call[1]["c"]}), call] if resume else [call]

    calls = [READ, *(c for f in files for c in posted(f))]
    if title:
        calls.append(("title", {"i": UUID, "t": title}))
    if subscribe:
        calls += [sub(e) for e in HUMANS]
    calls += posted(post("comment", body))
    if attach:
        calls.append(("attach", {"i": UUID, "u": attach, "t": attach_title}))
    return calls + [reread(), move(state)]


HANDOFF = (
    "Handoff from PRD-3: https://linear.app/t/issue/PRD-3\n\n## Source\n- PRD: x\n\n"
    "## Instructions\nBuild it.\n\n## Comments\n* Ann:\n  > Repo: ophis/other"
)


def issue(description):
    return Issue(
        UUID,
        ID,
        f"https://linear.app/t/issue/{ID}",
        "ENG: Session registry",
        description,
        "2026-09-01T00:00:00.000Z",
        None,
        (),
        (),
        (),
        handoff=issues.parse_handoff(description),
    )


class Gql:
    def __init__(
        self,
        states=(STATES["in_progress"],),
        attachments=(),
        fail=lambda _name, _v: None,
        issue=None,
        comments=None,
    ):
        self.calls, self.states, self.attachments, self.fail, self.issue = (
            [],
            list(states),
            attachments,
            fail,
            issue,
        )
        self.comments = dict(comments or {})

    def __call__(self, query, **v):
        name = NAMES[query]
        self.calls.append((name, v))
        r = self.fail(name, v)
        if isinstance(r, BaseException):
            raise r
        if name == "read":
            return {"issue": {"attachments": {"nodes": [{"url": u} for u in self.attachments]}}}
        if name == "reread":
            s = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"issue": {"state": {"id": s}}}
        if name == "id":
            return {"issue": self.issue}
        if name == "find":
            c = self.comments.get(v["c"])
            if c is None:
                raise NOT_FOUND
            user = {"isMe": c["mine"]}
            return {
                "comment": {
                    "id": v["c"],
                    "body": c["body"],
                    "issue": {"id": c["issue"]},
                    "user": user,
                }
            }
        if name == "comment" and "c" in v and r is not False:
            self.comments[v["c"]] = mine(v["b"], v["i"])
        return {FIELDS[name]: {"success": r is not False}}


class Base(Case):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        log.use_logs(os.path.join(self.tmp, "logs"))

    def ctx(self, role="engineer", *, gql=None):
        return writeback.Context(
            ident=ID,
            issue_id=UUID,
            role=role,
            sid=SID,
            resume=False,
            project=None,
            workdir=tempfile.mkdtemp(dir=self.tmp),
            gql=gql or Gql(),
            humans=HUMANS,
            states=STATES,
            repos=REPOS,
            team=board_ids.TEAM,
            target=TARGET,
        )

    def write(self, ctx, rel, text):
        path = os.path.join(ctx.workdir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def files(self, ctx):
        return (
            self.write(ctx, "src/agent-pm/docs/ENG-7-spec.md", SPEC_TEXT),
            self.write(ctx, "src/agent-pm/docs/ENG-7-plan.md", PLAN_TEXT),
        )

    def lines(self):
        events = logged()
        self.assertEqual(
            {(e["src"], e.get("sid", SID)) for e in events} - {("writeback", SID)}, set()
        )
        return [show({k: v for k, v in e.items() if k != "sid"}) for e in events]

    def ledger(self, ctx):
        with open(os.path.join(ctx.workdir, "writeback.json")) as f:
            return json.load(f)

    def steps(self, ctx):
        return {sid: entry["steps"] for sid, entry in self.ledger(ctx).items()}

    def no_ledger(self, ctx):
        self.assertFalse(os.path.exists(os.path.join(ctx.workdir, "writeback.json")))


class ProgressReporting(Base):
    def test_start_once_per_sid(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        start = Progress("start", "first build of the PRD")
        writeback.progress(ctx, start)
        writeback.progress(ctx, start)
        self.assertEqual(gql.calls, [post("start", "Build started: first build of the PRD")])
        self.assertEqual(self.steps(ctx), {SID: ["start"]})
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
        writeback.progress(
            ctx, Finished(Result(0, Outcome(status="done", summary="x", title="Queue options")))
        )
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
        self.assertEqual(self.steps(ctx), {SID: steps})

    def test_swallows_system_exit_and_retries_later(self):
        gql = Gql(fail=lambda _name, _v: LinearError("linear api error: down"))
        ctx = self.ctx(gql=gql)
        writeback.progress(ctx, START)
        self.assertEqual(
            self.lines(), [f"step-error {ID} step=start error=LinearError: linear api error: down"]
        )
        ok = Gql()
        writeback.progress(replace(ctx, gql=ok), START)
        self.assertEqual(ok.calls, [post("start", "Build started: x")])

    def test_signal_exit_passes_through(self):
        gql = Gql(fail=lambda _name, _v: Stopped(129))
        ctx = self.ctx(gql=gql)
        with self.assertRaises(Stopped) as cm:
            writeback.progress(ctx, START)
        self.assertEqual(cm.exception.code, 129)
        self.no_ledger(ctx)
        self.assertEqual(self.lines(), [])

    def test_never_raises(self):
        gql = Gql(fail=lambda _name, _v: RuntimeError("boom"))
        open(os.path.join(self.tmp, "file"), "w").close()
        with logs_at(os.path.join(self.tmp, "file", "logs")):
            writeback.progress(self.ctx(gql=gql), START)
        writeback.progress(replace(self.ctx(), role="nope"), START)
        self.assertEqual(self.lines(), [f"step-error {ID} step=sink error=KeyError: 'nope'"])
        (record,) = logged()
        self.assertIn("in progress", record["traceback"])
        self.assertIn("KeyError: 'nope'", record["traceback"])


class CompletionFailures(Base):
    def test_unknown_role_logged(self):
        self.assertFalse(
            writeback.finish(
                self.ctx("nope"), Outcome(status="done", summary="x", title="Queue options")
            )
        )
        self.assertEqual(self.lines(), [f"step-error {ID} step=finish error=KeyError: 'nope'"])
        (record,) = logged()
        self.assertIn("in finish", record["traceback"])
        self.assertIn("KeyError: 'nope'", record["traceback"])

    def test_signal_exit_stops_the_steps(self):
        for name in ("read", "subscribe", "comment", "attach"):
            with self.subTest(name=name):
                gql = Gql(fail=lambda n, _v, *, name=name: Stopped(143) if n == name else None)
                ctx = self.ctx(gql=gql)
                self.assertFalse(
                    writeback.finish(
                        ctx, Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
                    )
                )
                self.assertEqual(gql.calls[-1][0], name)
                self.assertEqual(
                    self.lines()[-1], f"step-error {ID} step=finish error=Stopped: 143"
                )
                self.assertNotIn("state", [c[0] for c in gql.calls])


class IssueBounce(Base):
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
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid(reason))
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
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid("r"))
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
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid(reason, mapped=True))
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
                    fail=lambda name, _v, *, err=err: err if name == "unarchive" else None,
                )
                writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid("r"))
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
        writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid("r"))
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
                writeback.bounce(self.ctx(gql=gql), issue(description), Invalid(reason))
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
        writeback.bounce(self.ctx(gql=gql), issue("x"), Invalid("r"))
        self.assertEqual(
            gql.calls[-3:],
            [
                comment(
                    self.question("r")
                    + "\n\nCould not subscribe ann@example.com: LinearError: issueSubscribe: success: false"
                ),
                reread(),
                move("in_review"),
            ],
        )

    def test_signal_exit_raises(self):
        for name in ("unarchive", "subscribe"):
            with self.subTest(name=name):
                gql = Gql(
                    issue=self.src(),
                    fail=lambda n, _v, *, name=name: Stopped(129) if n == name else None,
                )
                with self.assertRaises(Stopped):
                    writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid("r"))
                self.assertEqual(gql.calls[-1][0], name)

    def test_other_failures_raise(self):
        gql = Gql(issue=self.src(), fail=lambda name, _v: False if name == "comment" else None)
        with self.assertRaises(LinearError):
            writeback.bounce(self.ctx(gql=gql), issue(HANDOFF), Invalid("r"))
        self.assertEqual(gql.calls[-1], comment(self.text("r"), SRC_UUID))
        gql = Gql(
            fail=lambda name, _v: LinearError("linear api error: x") if name == "state" else None
        )
        with self.assertRaises(LinearError):
            writeback.bounce(self.ctx(gql=gql), issue("x"), Invalid("r"))


class WritebackContext(run_fixtures.Base):
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


class DurableRunNotes(Case):
    def test_one_durable_json_line_per_note(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "logs", "runs.jsonl")
        at = "2026-10-11T18:00:00+08:00"
        with mock.patch.object(os, "fsync", wraps=os.fsync) as spy:
            runs.note(path, "limit", issue=ID, sid=SID, client="claude", resets_at=None)
            runs.note(path, "limit", issue=ID, sid=OTHER_SID, client="claude", resets_at=at)
        self.assertEqual(spy.call_count, 4)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.endswith("\n"))
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(
            [list(r) for r in rows], [["ts", "kind", "issue", "sid", "client", "resets_at"]] * 2
        )
        for r in rows:
            ts = datetime.fromisoformat(r["ts"])
            self.assertIsNotNone(ts.utcoffset())
            self.assertEqual(r["ts"], ts.isoformat(timespec="seconds"))
        self.assertEqual(
            [{k: v for k, v in r.items() if k != "ts"} for r in rows],
            [
                {"kind": "limit", "issue": ID, "sid": SID, "client": "claude", "resets_at": None},
                {
                    "kind": "limit",
                    "issue": ID,
                    "sid": OTHER_SID,
                    "client": "claude",
                    "resets_at": at,
                },
            ],
        )


class UsageLimitReporting(Base):
    def setUp(self):
        super().setUp()
        # POSIX TZ: CST-8 is UTC+8
        tz = mock.patch.dict(os.environ, {"TZ": "CST-8"})
        tz.start()
        self.addCleanup(time.tzset)
        self.addCleanup(tz.stop)
        time.tzset()

    def test_body_names_the_reset_time_or_says_it_is_unknown(self):
        for resets_at, text in (
            (T, "Usage limit, resets at 2026-10-11T18:00:00+08:00."),
            (None, "Usage limit, reset time unknown."),
            (1e20, "Usage limit, reset time unknown."),
        ):
            with self.subTest(resets_at=resets_at):
                gql = Gql()
                ctx = self.ctx(gql=gql)
                self.assertTrue(writeback.limit(ctx, resets_at))
                self.assertEqual(
                    gql.calls,
                    [
                        sub(HUMANS[0]),
                        sub(HUMANS[1]),
                        post("limit", text),
                        reread(),
                        move("in_review"),
                    ],
                )
                self.assertEqual(self.steps(ctx), {SID: ["limit"]})

    def test_local_time(self):
        self.assertEqual(writeback.local_time(T), "2026-10-11T18:00:00+08:00")
        self.assertIsNone(writeback.local_time(None))

    def test_subscribes_the_humans_and_notes_failures(self):
        gql = Gql()
        self.assertTrue(writeback.limit(replace(self.ctx(gql=gql), humans=()), T))
        self.assertEqual(
            gql.calls,
            [
                post("limit", "Usage limit, resets at 2026-10-11T18:00:00+08:00."),
                reread(),
                move("in_review"),
            ],
        )
        gql = Gql(
            fail=lambda name, v: False if name == "subscribe" and v["e"] == HUMANS[0] else None
        )
        self.assertTrue(writeback.limit(self.ctx(gql=gql), None))
        self.assertEqual(
            gql.calls[2],
            post(
                "limit",
                (
                    "Usage limit, reset time unknown.\n\nCould not subscribe ann@example.com: LinearError: issueSubscribe: success: false"
                ),
            ),
        )

    def test_moves_to_in_review_only_from_in_progress(self):
        for state, skip in (
            ("in_review", None),
            ("handoff", f"skip {ID} reason=move to in_review: issue is {STATES['handoff']}"),
        ):
            with self.subTest(state=state):
                gql = Gql(states=[STATES[state]])
                before = len(logged())
                self.assertTrue(writeback.limit(self.ctx(gql=gql), T))
                self.assertEqual(gql.calls[-1], reread())
                self.assertEqual(
                    [line for line in self.lines()[before:] if line.startswith("skip")],
                    [skip] if skip else [],
                )

    def test_a_second_limit_on_the_sid_moves_again_without_a_second_comment(self):
        ctx = self.ctx()
        self.assertTrue(writeback.limit(ctx, T))
        again = Gql()
        self.assertTrue(writeback.limit(replace(ctx, gql=again, resume=True), None))
        self.assertEqual(again.calls, [sub(HUMANS[0]), sub(HUMANS[1]), reread(), move("in_review")])
        self.assertEqual(self.steps(ctx), {SID: ["limit"]})
        self.assertEqual(self.lines(), [f"step {ID} step=limit"])

    def test_a_failed_comment_stops_before_the_move(self):
        gql = Gql(fail=lambda name, _v: False if name == "comment" else None)
        ctx = self.ctx(gql=gql)
        self.assertFalse(writeback.limit(ctx, T))
        self.assertEqual(gql.calls[-1][0], "comment")
        self.assertEqual(
            self.lines(),
            [f"step-error {ID} step=limit error=LinearError: commentCreate: success: false"],
        )
        self.no_ledger(ctx)

    def test_never_raises(self):
        for name, err, line in (
            (
                "state",
                LinearError("linear api error: x"),
                "step=move:in_review error=LinearError: linear api error: x",
            ),
            ("reread", Stopped(143), "step=limit error=Stopped: 143"),
            ("subscribe", Stopped(143), "step=limit error=Stopped: 143"),
        ):
            with self.subTest(name=name):
                gql = Gql(fail=lambda n, _v, *, err=err, name=name: err if n == name else None)
                self.assertFalse(writeback.limit(self.ctx(gql=gql), T))
                self.assertEqual(gql.calls[-1][0], name)
                self.assertEqual(self.lines()[-1], f"step-error {ID} {line}")
                if "step=limit" in line:
                    record = logged()[-1]
                    self.assertIn("in limit", record["traceback"])
                    self.assertIn("Stopped: 143", record["traceback"])


class CompletionSteps(Base):
    def test_attach_skipped_when_already_attached(self):
        gql = Gql(attachments=[PR])
        ctx = self.ctx(gql=gql)
        self.assertTrue(
            writeback.finish(
                ctx, Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
            )
        )
        self.assertEqual(gql.calls, expect(body="Build ready: Ready.", state="in_review"))
        self.assertIn(f"attach:{PR}", self.steps(ctx)[SID])

    def test_already_linked_errors_count_as_attached(self):
        for text in (
            "url has already been linked to issue",
            "Duplicate attachment",
            "Unable to create issue attachment",
        ):
            with self.subTest(text=text):
                err = LinearError(f"linear api error: [{{'message': '{text}'}}]")
                gql = Gql(fail=lambda name, _v, *, err=err: err if name == "attach" else None)
                ctx = self.ctx(gql=gql)
                self.assertTrue(
                    writeback.finish(
                        ctx, Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
                    )
                )
                self.assertEqual(gql.calls[-1], move("in_review"))
                self.assertIn(f"attach:{PR}", self.steps(ctx)[SID])

    def test_other_attach_error_stops(self):
        gql = Gql(
            fail=lambda name, _v: (
                LinearError("linear api error: boom") if name == "attach" else None
            )
        )
        ctx = self.ctx(gql=gql)
        self.assertFalse(
            writeback.finish(
                ctx, Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
            )
        )
        self.assertEqual(gql.calls[-1][0], "attach")
        self.assertEqual(
            self.lines()[-1],
            f"step-error {ID} step=attach:{PR} error=LinearError: linear api error: boom",
        )

    def test_no_move_from_another_state(self):
        for state, skip in (
            ("in_review", None),
            ("handoff", f"skip {ID} reason=move to in_review: issue is {STATES['handoff']}"),
        ):
            with self.subTest(state=state):
                gql = Gql(states=[STATES[state]])
                ctx = self.ctx(gql=gql)
                self.assertTrue(
                    writeback.finish(
                        ctx,
                        Outcome(status="failed", title="ENG-7: x", summary="push not permitted"),
                    )
                )
                self.assertEqual(gql.calls[-1], reread())
                self.assertEqual(
                    [line for line in self.lines() if line.startswith("skip")],
                    [skip] if skip else [],
                )
                self.assertEqual(self.lines()[-1], f"step {ID} step=move:in_review")
                self.assertIn("move:in_review", self.steps(ctx)[SID])

    def test_subscribe_failures_noted_and_finish_continues(self):
        def fail(name, v):
            if name == "subscribe":
                return False if v["e"] == HUMANS[0] else LinearError("linear api error: boom")

        gql = Gql(fail=fail)
        ctx = self.ctx(gql=gql)
        self.assertTrue(
            writeback.finish(
                ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
            )
        )
        body = (
            "Build failed: push not permitted"
            "\n\nCould not subscribe ann@example.com: LinearError: issueSubscribe: success: false"
            "\n\nCould not subscribe bob@example.com: LinearError: linear api error: boom"
        )
        self.assertEqual(gql.calls, expect(body=body, state="in_review"))

    def test_rejected_spec_post_noted(self):
        err = LinearError("linear api error: body too long")
        gql = Gql(
            fail=lambda name, v: (
                err if name == "comment" and v["b"].startswith("**Spec**") else None
            )
        )
        ctx = self.ctx(gql=gql)
        spec, plan = self.files(ctx)
        self.assertTrue(
            writeback.finish(
                ctx,
                Outcome(
                    status="done",
                    title="ENG-7: x",
                    summary="Ready.",
                    url=PR,
                    files=list([spec, plan]),
                ),
            )
        )
        body = "Build ready: Ready.\n\nCould not post the Spec `ENG-7-spec.md`: LinearError: linear api error: body too long"
        self.assertEqual(
            gql.calls,
            expect(
                files=[spec_comment(), plan_comment()],
                body=body,
                state="in_review",
                attach=PR,
                attach_title="ENG-7: x",
            ),
        )
        self.assertIn(
            (
                f"step-error {ID} step=file:ENG-7-spec.md:{sha(SPEC_TEXT)} error=LinearError: linear api error: body too long"
            ),
            self.lines(),
        )


class OutcomeFiles(Base):
    def finish_with(self, ctx, path):
        gql = Gql()
        writeback.finish(
            replace(ctx, gql=gql),
            Outcome(
                status="failed", title="ENG-7: x", summary="push not permitted", files=list([path])
            ),
        )
        posted = [c for c in gql.calls if c[0] == "comment"]
        return gql, posted[-1][1]["b"]

    def symlink(self, ctx):
        spec, _ = self.files(ctx)
        os.symlink(spec, os.path.join(ctx.workdir, "link.md"))
        return os.path.join(ctx.workdir, "link.md")

    def hard_link(self, ctx):
        spec, _ = self.files(ctx)
        os.link(spec, os.path.join(ctx.workdir, "copy.md"))
        return spec

    def outside(self, ctx):
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w") as f:
            f.write("secret")
        return outside

    def linked_dir(self, ctx):
        os.makedirs(os.path.join(self.tmp, "away"))
        with open(os.path.join(self.tmp, "away", "x.md"), "w") as f:
            f.write("secret")
        os.symlink(os.path.join(self.tmp, "away"), os.path.join(ctx.workdir, "away"))
        return os.path.join(ctx.workdir, "away", "x.md")

    def fifo(self, ctx):
        os.mkfifo(os.path.join(ctx.workdir, "pipe.md"))
        return os.path.join(ctx.workdir, "pipe.md")

    def test_skipped(self):
        for name, make, reason in (
            ("symlink", self.symlink, "OSError: "),
            ("hard link", self.hard_link, "ValueError: not a single-link file"),
            ("not md", lambda ctx: self.write(ctx, "notes.txt", "x"), "ValueError: not a .md file"),
            ("outside the workdir", self.outside, "ValueError: not under the workdir"),
            ("outside through a linked dir", self.linked_dir, "ValueError: not under the workdir"),
            (
                "too big",
                lambda ctx: self.write(ctx, "big.md", "x" * 1_000_001),
                "ValueError: over 1000000 bytes",
            ),
            ("fifo, without blocking", self.fifo, "ValueError: not a regular file"),
        ):
            with self.subTest(name):
                ctx = self.ctx()
                path = make(ctx)
                gql, body = self.finish_with(ctx, path)
                self.assertEqual(
                    [c for c in gql.calls if c[0] == "comment"], [post("comment", body)]
                )
                note = f"Build failed: push not permitted\n\nCould not post the file `{os.path.basename(path)}`: {reason}"
                self.assertTrue(
                    body.startswith(note) if reason == "OSError: " else body == note, body
                )

    def test_posted(self):
        def invalid_utf8(ctx):
            path = os.path.join(ctx.workdir, "spec.md")
            with open(path, "wb") as f:
                f.write(b"ok \xff")
            return path

        for name, make, data, posted in (
            (
                "at the limit",
                lambda ctx: self.write(ctx, "big.md", "x" * 1_000_000),
                b"x" * 1_000_000,
                f"**Spec** `big.md`\n\n---\n\n{'x' * 1_000_000}",
            ),
            (
                "invalid utf-8 replaced",
                invalid_utf8,
                b"ok \xff",
                "**Spec** `spec.md`\n\n---\n\nok �",
            ),
        ):
            with self.subTest(name):
                ctx = self.ctx()
                path = make(ctx)
                gql, _ = self.finish_with(ctx, path)
                step = f"file:{os.path.basename(path)}:{hashlib.sha256(data).hexdigest()[:12]}"
                self.assertEqual(gql.calls[1], post(step, posted))


class EngineeringUrls(Base):
    def test_an_engineering_url_must_be_on_the_target(self):
        evil = "https://github.com/ophis/agent-pm-evil/pull/1"
        for name, target, o, calls, skip in (
            (
                "other repo dropped",
                TARGET,
                Outcome(status="done", title="ENG-7: x", summary="Ready.", url=evil),
                expect(body="Build ready: Ready.", state="in_review"),
                f"skip {ID} reason=url not on ophis/agent-pm",
            ),
            (
                "case-insensitive match",
                ("Ophis", "Agent-PM"),
                Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR),
                expect(
                    body="Build ready: Ready.",
                    state="in_review",
                    attach=PR,
                    attach_title="ENG-7: x",
                ),
                None,
            ),
            (
                "no target drops every url",
                None,
                Outcome(status="failed", title="ENG-7: x", summary="push not permitted", url=TREE),
                expect(body="Build failed: push not permitted", state="in_review"),
                f"skip {ID} reason=url not on a target repo",
            ),
        ):
            with self.subTest(name):
                gql = Gql()
                before = len(logged())
                writeback.finish(replace(self.ctx(gql=gql), target=target), o)
                self.assertEqual(gql.calls, calls)
                self.assertEqual(
                    [line for line in self.lines()[before:] if line.startswith("skip ")],
                    [skip] if skip else [],
                )


class ApprovalMessages(Base):
    def test_say(self):
        self.assertEqual(writeback.say("", "just text"), "just text")
        self.assertEqual(writeback.say("Build started:", ""), "Build started")
        self.assertEqual(
            writeback.say("Build started:", "first build of the PRD"),
            "Build started: first build of the PRD",
        )
        self.assertEqual(writeback.say("Build ready:", "a\nb"), "Build ready:\na\nb")

    def test_approve_line(self):
        self.assertEqual(writeback.approve_line(None), APPROVE)
        self.assertEqual(writeback.approve_line("ophis/agent-pm"), APPROVE_MAPPED)

    def test_queries_verbatim(self):
        self.assertEqual(
            writeback.M_TITLE,
            (
                "mutation($i: String!, $t: String!) { issueUpdate(id: $i, input: { title: $t }) { success } }"
            ),
        )
        self.assertEqual(
            writeback.M_ATTACH,
            (
                "mutation($i: String!, $u: String!, $t: String) { attachmentLinkURL(issueId: $i, url: $u, title: $t) { success } }"
            ),
        )
        self.assertEqual(
            writeback.M_UNARCHIVE, "mutation($i: String!) { issueUnarchive(id: $i) { success } }"
        )
        self.assertEqual(
            writeback.Q_ATTACHED,
            "query($i: String!) { issue(id: $i) { attachments(first: 50) { nodes { url } } } }",
        )
        self.assertEqual(writeback.Q_ID, "query($i: String!) { issue(id: $i) { id team { id } } }")


class CompletionMessages(Base):
    def outcomes(self, role, spec, plan):
        kind, files = config.TASKS[role].kind, [plan, spec]
        if kind == "research":
            return {
                "done": Outcome(
                    status="done",
                    summary="Three queues compared.\nRedis streams fit best.",
                    url=DOC,
                    files=list(files),
                    title="Queue options",
                ),
                "needs_input": Outcome(
                    status="needs_input",
                    questions=list(["Which repo?", "Which version?"]),
                    url=DOC,
                    files=list(files),
                    title="Queue options",
                    summary="",
                ),
                "failed": Outcome(
                    status="failed",
                    summary="gh api failed",
                    url=DOC,
                    files=list(files),
                    title="Queue options",
                ),
            }
        if kind == "design":
            return {
                "done": Outcome(
                    status="done",
                    title="Session Registry",
                    summary="PRD for the registry.",
                    url=PRD,
                    files=list(files),
                ),
                "needs_input": Outcome(
                    status="needs_input",
                    title="Session Registry",
                    questions=list(["Who uses it?", "Web or CLI?"]),
                    files=list(files),
                    summary="",
                ),
                "failed": Outcome(
                    status="failed",
                    title="Session Registry",
                    summary="Could not publish.",
                    files=list(files),
                ),
            }
        return {
            "done": Outcome(
                status="done",
                title="ENG-7: Session registry",
                summary="Adds the registry.\nVerify: python3 -m unittest",
                url=PR,
                files=list(files),
            ),
            "needs_input": Outcome(
                status="needs_input",
                title="ENG-7: Session registry",
                questions=list(["Keep the old API?"]),
                files=list(files),
                summary="",
            ),
            "failed": Outcome(
                status="failed",
                title="ENG-7: Session registry",
                summary="push not permitted",
                url=TREE,
                files=list(files),
            ),
        }

    def golden(self, role, status, resume):
        research = (
            "1. Which repo?\n2. Which version?\n\n"
            + FOOTER
            + " To change the target repo, edit the description's `Repo:` line."
        )
        files = [spec_comment(), plan_comment()] if config.TASKS[role].files else []
        table = {
            ("research", "done"): expect(
                resume=resume,
                body=f"Three queues compared.\nRedis streams fit best.\n\n{DOC}",
                state="in_review",
                attach=DOC,
                attach_title="Queue options",
            ),
            ("research", "needs_input"): expect(resume=resume, body=research, state="in_review"),
            ("research", "failed"): (
                expect(resume=resume, body=f"gh api failed\n\n{DOC}", state="in_review")
                if resume
                else expect(
                    resume=resume, body=f"gh api failed\n\n{DOC}", state="todo", subscribe=False
                )
            ),
            ("design", "done"): expect(
                resume=resume,
                title="PRD: Session Registry",
                body=f"PRD for the registry.\n\n{PRD}\n\n{APPROVE}",
                state="in_review",
                attach=PRD,
                attach_title="Session Registry",
            ),
            ("design", "needs_input"): expect(
                resume=resume,
                body=f"1. Who uses it?\n2. Web or CLI?\n\n{FOOTER}",
                state="in_review",
            ),
            ("design", "failed"): expect(
                resume=resume, body="Could not publish.", state="in_review"
            ),
            ("build", "done"): expect(
                resume=resume,
                files=files,
                state="in_review",
                body="Build ready:\nAdds the registry.\nVerify: python3 -m unittest",
                attach=PR,
                attach_title="ENG-7: Session registry",
            ),
            ("build", "needs_input"): expect(
                resume=resume,
                body=f"Question:\n1. Keep the old API?\n\n{FOOTER}",
                state="in_review",
            ),
            ("build", "failed"): expect(
                resume=resume,
                files=files,
                state="in_review",
                body=f"Build failed: push not permitted\n\n{TREE}",
            ),
        }
        return table[(config.TASKS[role].kind, status)]

    def test_every_role_status_and_mode(self):
        self.assertEqual(set(config.TASKS), {"researcher", "pm", "engineer"})
        for role in config.TASKS:
            for status in STATUSES:
                for resume in (False, True):
                    with self.subTest(role=role, status=status, resume=resume):
                        gql = Gql()
                        build = config.TASKS[role].kind == "build"
                        ctx = replace(
                            self.ctx(role, gql=gql), resume=resume, target=TARGET if build else None
                        )
                        spec, plan = self.files(ctx)
                        self.assertTrue(
                            writeback.finish(ctx, self.outcomes(role, spec, plan)[status])
                        )
                        self.assertEqual(gql.calls, self.golden(role, status, resume))

    def test_log_and_ledger(self):
        ctx = self.ctx()
        spec, plan = self.files(ctx)
        writeback.finish(ctx, self.outcomes("engineer", spec, plan)["done"])
        steps = [
            f"file:ENG-7-spec.md:{sha(SPEC_TEXT)}",
            f"file:ENG-7-plan.md:{sha(PLAN_TEXT)}",
            "comment",
            f"attach:{PR}",
            "move:in_review",
        ]
        self.assertEqual(self.lines(), [f"step {ID} step={s}" for s in steps])
        self.assertEqual([e["sid"] for e in logged()], [SID] * len(steps))
        self.assertEqual(self.ledger(ctx), {SID: {"steps": steps}})

    def test_design_retitle_and_mapped_approve_line(self):
        gql = Gql()
        ctx = replace(self.ctx("pm", gql=gql), target=None, project=PROJECT)
        writeback.finish(
            ctx,
            Outcome(
                status="done", title="Session Registry", summary="PRD for the registry.", url=PRD
            ),
        )
        self.assertEqual(
            gql.calls,
            expect(
                title="PRD: Session Registry",
                body=f"PRD for the registry.\n\n{PRD}\n\n{APPROVE_MAPPED}",
                state="in_review",
                attach=PRD,
                attach_title="Session Registry",
            ),
        )
        self.assertEqual(
            self.steps(ctx), {SID: ["retitle", "comment", f"attach:{PRD}", "move:in_review"]}
        )


class CompletionLedger(Base):
    def test_second_finish_skips_ledgered_steps(self):
        ctx = self.ctx()
        spec, plan = self.files(ctx)
        done = Outcome(
            status="done", title="ENG-7: x", summary="Ready.", url=PR, files=list([spec, plan])
        )
        self.assertTrue(writeback.finish(ctx, done))
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, gql=again), done))
        self.assertEqual(again.calls, [READ, sub(HUMANS[0]), sub(HUMANS[1])])

    def test_stops_at_first_error_and_resumes_there(self):
        broken = Gql(
            fail=lambda name, v: (
                False if name == "comment" and v["b"].startswith("Build ready") else None
            )
        )
        ctx = self.ctx(gql=broken)
        spec, plan = self.files(ctx)
        done = Outcome(
            status="done", title="ENG-7: x", summary="Ready.", url=PR, files=list([spec, plan])
        )
        self.assertFalse(writeback.finish(ctx, done))
        self.assertEqual(broken.calls[-1], post("comment", "Build ready: Ready."))
        self.assertEqual(
            self.lines()[-1],
            f"step-error {ID} step=comment error=LinearError: commentCreate: success: false",
        )
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, gql=again), done))
        self.assertEqual(
            again.calls,
            expect(
                body="Build ready: Ready.", state="in_review", attach=PR, attach_title="ENG-7: x"
            ),
        )

    def test_read_error_stops(self):
        gql = Gql(
            fail=lambda name, _v: LinearError("linear api error: down") if name == "read" else None
        )
        ctx = self.ctx(gql=gql)
        self.assertFalse(
            writeback.finish(
                ctx, Outcome(status="done", title="ENG-7: x", summary="Ready.", url=PR)
            )
        )
        self.assertEqual(gql.calls, [READ])
        self.assertEqual(
            self.lines(), [f"step-error {ID} step=read error=LinearError: linear api error: down"]
        )

    def test_other_sid_does_not_skip(self):
        ctx = self.ctx()
        self.write(
            ctx,
            "writeback.json",
            json.dumps({OTHER_SID: {"steps": ["comment", "move:in_review"]}}),
        )
        gql = Gql()
        writeback.finish(
            replace(ctx, gql=gql),
            Outcome(status="failed", title="ENG-7: x", summary="push not permitted"),
        )
        self.assertEqual(
            gql.calls,
            expect(body="Build failed: push not permitted", state="in_review"),
        )
        self.assertEqual(
            self.steps(ctx),
            {OTHER_SID: ["comment", "move:in_review"], SID: ["comment", "move:in_review"]},
        )

    def test_unreadable_ledger_is_empty(self):
        for text in (
            "not json",
            json.dumps([1]),
            json.dumps({SID: "comment move:in_review"}),
            json.dumps({SID: [1]}),
            json.dumps({SID: ["comment", "move:in_review"]}),
            json.dumps({SID: {"steps": "comment move:in_review"}}),
            json.dumps({SID: {"steps": [1]}}),
            json.dumps({SID: {"steps": ["comment", "move:in_review"], "done": True}}),
        ):
            with self.subTest(text=text):
                gql = Gql()
                ctx = self.ctx(gql=gql)
                self.write(ctx, "writeback.json", text)
                writeback.finish(
                    ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
                )
                self.assertEqual(
                    gql.calls,
                    expect(body="Build failed: push not permitted", state="in_review"),
                )

    def test_fifo_ledger_does_not_block(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        os.mkfifo(os.path.join(ctx.workdir, "writeback.json"))
        self.assertTrue(
            writeback.finish(
                ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
            )
        )
        self.assertEqual(self.steps(ctx), {SID: ["comment", "move:in_review"]})

    def test_symlinked_ledger_is_empty_and_replaced(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        elsewhere = os.path.join(self.tmp, "elsewhere.json")
        with open(elsewhere, "w") as f:
            f.write(json.dumps({SID: {"steps": ["comment", "move:in_review"]}}))
        os.symlink(elsewhere, os.path.join(ctx.workdir, "writeback.json"))
        writeback.finish(
            ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
        )
        self.assertEqual(
            gql.calls,
            expect(body="Build failed: push not permitted", state="in_review"),
        )
        self.assertFalse(os.path.islink(os.path.join(ctx.workdir, "writeback.json")))
        with open(elsewhere) as f:
            self.assertEqual(json.load(f), {SID: {"steps": ["comment", "move:in_review"]}})

    def test_ledger_write_failure_leaves_no_temp(self):
        ctx = self.ctx()
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            writeback.progress(ctx, START)
        self.assertEqual(self.lines(), [f"step-error {ID} step=start error=OSError: disk full"])
        self.assertEqual(ctx.gql.calls, [post("start", "Build started: x")])
        self.assertEqual(os.listdir(ctx.workdir), [])


class ResumedComments(Base):
    def test_a_new_run_never_queries(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        writeback.progress(ctx, START)
        self.assertTrue(
            writeback.finish(
                ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
            )
        )
        self.assertTrue(writeback.limit(ctx, None))
        self.assertNotIn("find", [c[0] for c in gql.calls])
        self.assertEqual(
            [c for c in gql.calls if c[0] == "comment"],
            [
                post("start", "Build started: x"),
                post("comment", "Build failed: push not permitted"),
                post("limit", "Usage limit, reset time unknown."),
            ],
        )

    def test_a_resume_skips_ledgered_steps_without_a_query(self):
        ctx = self.ctx()
        done = Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
        writeback.progress(ctx, START)
        self.assertTrue(writeback.finish(ctx, done))
        again = Gql()
        resumed = replace(ctx, gql=again, resume=True)
        writeback.progress(resumed, START)
        self.assertTrue(writeback.finish(resumed, done))
        self.assertEqual(again.calls, [READ, sub(HUMANS[0]), sub(HUMANS[1])])

    def test_a_resume_finds_a_post_whose_ledger_write_was_lost(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            writeback.progress(ctx, START)
        self.assertEqual(gql.calls, [post("start", "Build started: x")])
        self.assertEqual(self.lines(), [f"step-error {ID} step=start error=OSError: disk full"])
        self.no_ledger(ctx)
        gql.calls.clear()
        writeback.progress(replace(ctx, resume=True), START)
        self.assertEqual(gql.calls, [find("start")])
        self.assertEqual(self.steps(ctx), {SID: ["start"]})
        self.assertEqual(self.lines()[-1], f"step {ID} step=start found=true")

    def test_a_resumed_finish_counts_a_found_comment_as_posted(self):
        body = "Build failed: push not permitted"
        gql = Gql(comments={cid("comment"): mine(body)})
        ctx = replace(self.ctx(gql=gql), resume=True)
        self.assertTrue(
            writeback.finish(
                ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
            )
        )
        self.assertEqual(
            gql.calls,
            [READ, sub(HUMANS[0]), sub(HUMANS[1]), find("comment"), reread(), move("in_review")],
        )
        self.assertEqual(self.steps(ctx), {SID: ["comment", "move:in_review"]})
        self.assertEqual(
            self.lines(), [f"step {ID} step=comment found=true", f"step {ID} step=move:in_review"]
        )

    def test_a_resume_after_a_failed_post_finds_then_posts_once(self):
        broken = Gql(fail=lambda name, _v: False if name == "comment" else None)
        ctx = self.ctx(gql=broken)
        done = Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
        self.assertFalse(writeback.finish(ctx, done))
        again = Gql()
        self.assertTrue(writeback.finish(replace(ctx, gql=again, resume=True), done))
        self.assertEqual(
            again.calls,
            expect(body="Build failed: push not permitted", state="in_review", resume=True),
        )
        self.assertEqual(len([c for c in again.calls if c[0] == "comment"]), 1)

    def test_a_find_error_is_the_steps_error(self):
        squatted = f"LinearError: comment {cid('comment')} is not ours"
        for name, comments, fail, error in (
            ("another issue's", {cid("comment"): mine("x", OTHER_ISSUE)}, None, squatted),
            (
                "another author's",
                {cid("comment"): {"body": "x", "issue": UUID, "mine": False}},
                None,
                squatted,
            ),
            (
                "unreadable",
                {},
                LinearError("linear api error: down"),
                "LinearError: linear api error: down",
            ),
        ):
            with self.subTest(name):
                gql = Gql(
                    comments=comments,
                    fail=lambda n, _v, *, fail=fail: fail if n == "find" else None,
                )
                ctx = replace(self.ctx(gql=gql), resume=True)
                self.assertFalse(
                    writeback.finish(
                        ctx,
                        Outcome(status="failed", title="ENG-7: x", summary="push not permitted"),
                    )
                )
                self.assertEqual(gql.calls, [READ, sub(HUMANS[0]), sub(HUMANS[1]), find("comment")])
                self.assertEqual(self.lines()[-1], f"step-error {ID} step=comment error={error}")
                self.no_ledger(ctx)
