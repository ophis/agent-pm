import os
from dataclasses import replace
from unittest import mock

from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit.writeback.comment_ids_fixtures import (
    CommentIds as CommentIdsBase,
)
from orchestrator_test_helpers.unit.writeback.data import (
    HUMANS,
    ID,
    OTHER_ISSUE,
    READ,
    SID,
    START,
    UUID,
    cid,
    expect,
    find,
    mine,
    move,
    outcome,
    post,
    reread,
    sub,
)
from orchestrator_test_helpers.unit.writeback.gql_fixtures import Gql


class CommentIds1(CommentIdsBase):
    def test_a_new_run_never_queries(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        writeback.progress(ctx, START)
        self.assertTrue(writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted")))
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
        done = outcome("failed", "ENG-7: x", "push not permitted")
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
        ctx = self.ctx(gql=gql, resume=True)
        self.assertTrue(writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted")))
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
        done = outcome("failed", "ENG-7: x", "push not permitted")
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
                gql = Gql(comments=comments, fail=lambda n, _v: fail if n == "find" else None)
                ctx = self.ctx(gql=gql, resume=True)
                self.assertFalse(
                    writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted"))
                )
                self.assertEqual(gql.calls, [READ, sub(HUMANS[0]), sub(HUMANS[1]), find("comment")])
                self.assertEqual(self.lines()[-1], f"step-error {ID} step=comment error={error}")
                self.no_ledger(ctx)
