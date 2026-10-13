import hashlib
import os
import time
from dataclasses import replace
from unittest import mock

from agent_pm.linear_orchestrator.engine.types import LinearError, Stopped
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_tests.unit.run.support import logged
from orchestrator_tests.unit.writeback.data import (
    HUMANS,
    ID,
    SID,
    SPEC_TEXT,
    STATES,
    TARGET,
    TREE,
    T,
    expect,
    move,
    outcome,
    plan_comment,
    post,
    reread,
    sha,
    spec_comment,
    sub,
)
from orchestrator_tests.unit.writeback.fakes import Gql
from orchestrator_tests.unit.writeback.fixtures import Base
from orchestrator_tests.unit.writeback.support import PR


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
                self.assertEqual(self.ledger(ctx), {SID: ["limit"]})

    def test_local_time(self):
        self.assertEqual(writeback.local_time(T), "2026-10-11T18:00:00+08:00")
        self.assertIsNone(writeback.local_time(None))

    def test_subscribes_the_humans_and_notes_failures(self):
        gql = Gql()
        self.assertTrue(writeback.limit(self.ctx(gql=gql, humans=()), T))
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
                    "Usage limit, reset time unknown.\n\nCould not subscribe "
                    "ann@example.com: LinearError: issueSubscribe: success: false"
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
        self.assertEqual(self.ledger(ctx), {SID: ["limit"]})
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


class CompletionSteps(Base):
    def test_attach_skipped_when_already_attached(self):
        gql = Gql(attachments=[PR])
        ctx = self.ctx(gql=gql)
        self.assertTrue(writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR)))
        self.assertEqual(gql.calls, expect(body="Build ready: Ready.", state="in_review"))
        self.assertIn(f"attach:{PR}", self.ledger(ctx)[SID])

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
                    writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR))
                )
                self.assertEqual(gql.calls[-1], move("in_review"))
                self.assertIn(f"attach:{PR}", self.ledger(ctx)[SID])

    def test_other_attach_error_stops(self):
        gql = Gql(
            fail=lambda name, _v: (
                LinearError("linear api error: boom") if name == "attach" else None
            )
        )
        ctx = self.ctx(gql=gql)
        self.assertFalse(writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR)))
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
                    writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted"))
                )
                self.assertEqual(gql.calls[-1], reread())
                self.assertEqual(
                    [line for line in self.lines() if line.startswith("skip")],
                    [skip] if skip else [],
                )
                self.assertEqual(self.lines()[-1], f"step {ID} step=move:in_review")
                self.assertIn("move:in_review", self.ledger(ctx)[SID])

    def test_subscribe_failures_noted_and_finish_continues(self):
        def fail(name, v):
            if name == "subscribe":
                return False if v["e"] == HUMANS[0] else LinearError("linear api error: boom")

        gql = Gql(fail=fail)
        ctx = self.ctx(gql=gql)
        self.assertTrue(writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted")))
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
            writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR, files=[spec, plan]))
        )
        body = (
            "Build ready: Ready.\n\nCould not post the Spec `ENG-7-spec.md`: "
            "LinearError: linear api error: body too long"
        )
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
                f"step-error {ID} step=file:ENG-7-spec.md:{sha(SPEC_TEXT)}"
                f" error=LinearError: linear api error: body too long"
            ),
            self.lines(),
        )


class OutcomeFiles(Base):
    def finish_with(self, ctx, path):
        gql = Gql()
        writeback.finish(
            replace(ctx, gql=gql), outcome("failed", "ENG-7: x", "push not permitted", files=[path])
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

    def outside(self, _ctx):
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
            ("hard link", self.hard_link, "FileError: not a single-link file"),
            ("not md", lambda ctx: self.write(ctx, "notes.txt", "x"), "FileError: not a .md file"),
            ("outside the workdir", self.outside, "FileError: not under the workdir"),
            ("outside through a linked dir", self.linked_dir, "FileError: not under the workdir"),
            (
                "too big",
                lambda ctx: self.write(ctx, "big.md", "x" * 1_000_001),
                "FileError: over 1000000 bytes",
            ),
            ("fifo, without blocking", self.fifo, "FileError: not a regular file"),
        ):
            with self.subTest(name):
                ctx = self.ctx()
                path = make(ctx)
                gql, body = self.finish_with(ctx, path)
                self.assertEqual(
                    [c for c in gql.calls if c[0] == "comment"], [post("comment", body)]
                )
                note = (
                    f"Build failed: push not permitted\n\nCould not post the file `"
                    f"{os.path.basename(path)}`: {reason}"
                )
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
                outcome("done", "ENG-7: x", "Ready.", url=evil),
                expect(body="Build ready: Ready.", state="in_review"),
                f"skip {ID} reason=url not on ophis/agent-pm",
            ),
            (
                "case-insensitive match",
                ("Ophis", "Agent-PM"),
                outcome("done", "ENG-7: x", "Ready.", url=PR),
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
                outcome("failed", "ENG-7: x", "push not permitted", url=TREE),
                expect(body="Build failed: push not permitted", state="in_review"),
                f"skip {ID} reason=url not on a target repo",
            ),
        ):
            with self.subTest(name):
                gql = Gql()
                before = len(logged())
                writeback.finish(self.ctx(gql=gql, target=target), o)
                self.assertEqual(gql.calls, calls)
                self.assertEqual(
                    [line for line in self.lines()[before:] if line.startswith("skip ")],
                    [skip] if skip else [],
                )
