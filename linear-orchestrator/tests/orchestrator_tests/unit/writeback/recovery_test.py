import json
import os
from dataclasses import replace
from unittest import mock

from agent_pm.delegate import Outcome
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_tests.unit.writeback.data import (
    HUMANS,
    ID,
    OTHER_ISSUE,
    OTHER_SID,
    READ,
    SID,
    START,
    UUID,
    WritebackEffects,
    cid,
    expect,
    find,
    mine,
    move,
    post,
    reread,
    sub,
)
from orchestrator_tests.unit.writeback.fakes import Gql
from orchestrator_tests.unit.writeback.fixtures import Base
from orchestrator_tests.unit.writeback.support import PR


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
                facts=WritebackEffects(
                    body="Build ready: Ready.",
                    state="in_review",
                    attach=PR,
                    attach_title="ENG-7: x",
                )
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
        self.write(ctx, "writeback.json", json.dumps({OTHER_SID: ["comment", "move:in_review"]}))
        gql = Gql()
        writeback.finish(
            replace(ctx, gql=gql),
            Outcome(status="failed", title="ENG-7: x", summary="push not permitted"),
        )
        self.assertEqual(
            gql.calls,
            expect(
                facts=WritebackEffects(body="Build failed: push not permitted", state="in_review")
            ),
        )
        self.assertEqual(
            self.ledger(ctx),
            {OTHER_SID: ["comment", "move:in_review"], SID: ["comment", "move:in_review"]},
        )

    def test_unreadable_ledger_is_empty(self):
        for text in (
            "not json",
            json.dumps([1]),
            json.dumps({SID: "comment move:in_review"}),
            json.dumps({SID: [1]}),
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
                    expect(
                        facts=WritebackEffects(
                            body="Build failed: push not permitted", state="in_review"
                        )
                    ),
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
        self.assertEqual(self.ledger(ctx), {SID: ["comment", "move:in_review"]})

    def test_symlinked_ledger_is_empty_and_replaced(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        elsewhere = os.path.join(self.tmp, "elsewhere.json")
        with open(elsewhere, "w") as f:
            f.write(json.dumps({SID: ["comment", "move:in_review"]}))
        os.symlink(elsewhere, os.path.join(ctx.workdir, "writeback.json"))
        writeback.finish(
            ctx, Outcome(status="failed", title="ENG-7: x", summary="push not permitted")
        )
        self.assertEqual(
            gql.calls,
            expect(
                facts=WritebackEffects(body="Build failed: push not permitted", state="in_review")
            ),
        )
        self.assertFalse(os.path.islink(os.path.join(ctx.workdir, "writeback.json")))
        with open(elsewhere) as f:
            self.assertEqual(json.load(f), {SID: ["comment", "move:in_review"]})

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
        self.assertEqual(self.ledger(ctx), {SID: ["start"]})
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
        self.assertEqual(self.ledger(ctx), {SID: ["comment", "move:in_review"]})
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
            expect(
                facts=WritebackEffects(
                    body="Build failed: push not permitted", state="in_review", resume=True
                )
            ),
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
