import json
import os
from dataclasses import replace
from unittest import mock

from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit.writeback.data import (
    HUMANS,
    ID,
    OTHER_SID,
    READ,
    SID,
    expect,
    outcome,
    post,
    sub,
)
from orchestrator_test_helpers.unit.writeback.finish_ledger_fixtures import (
    FinishLedger as FinishLedgerBase,
)
from orchestrator_test_helpers.unit.writeback.gql_fixtures import Gql
from orchestrator_test_helpers.unit.writeback_support import PR


class FinishLedger1(FinishLedgerBase):
    def test_second_finish_skips_ledgered_steps(self):
        ctx = self.ctx()
        spec, plan = self.files(ctx)
        done = outcome("done", "ENG-7: x", "Ready.", url=PR, files=[spec, plan])
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
        done = outcome("done", "ENG-7: x", "Ready.", url=PR, files=[spec, plan])
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
        self.assertFalse(writeback.finish(ctx, outcome("done", "ENG-7: x", "Ready.", url=PR)))
        self.assertEqual(gql.calls, [READ])
        self.assertEqual(
            self.lines(), [f"step-error {ID} step=read error=LinearError: linear api error: down"]
        )

    def test_other_sid_does_not_skip(self):
        ctx = self.ctx()
        self.write(ctx, "writeback.json", json.dumps({OTHER_SID: ["comment", "move:in_review"]}))
        gql = Gql()
        writeback.finish(replace(ctx, gql=gql), outcome("failed", "ENG-7: x", "push not permitted"))
        self.assertEqual(
            gql.calls, expect(body="Build failed: push not permitted", state="in_review")
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
                writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted"))
                self.assertEqual(
                    gql.calls, expect(body="Build failed: push not permitted", state="in_review")
                )

    def test_fifo_ledger_does_not_block(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        os.mkfifo(os.path.join(ctx.workdir, "writeback.json"))
        self.assertTrue(writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted")))
        self.assertEqual(self.ledger(ctx), {SID: ["comment", "move:in_review"]})

    def test_symlinked_ledger_is_empty_and_replaced(self):
        gql = Gql()
        ctx = self.ctx(gql=gql)
        elsewhere = os.path.join(self.tmp, "elsewhere.json")
        with open(elsewhere, "w") as f:
            f.write(json.dumps({SID: ["comment", "move:in_review"]}))
        os.symlink(elsewhere, os.path.join(ctx.workdir, "writeback.json"))
        writeback.finish(ctx, outcome("failed", "ENG-7: x", "push not permitted"))
        self.assertEqual(
            gql.calls, expect(body="Build failed: push not permitted", state="in_review")
        )
        self.assertFalse(os.path.islink(os.path.join(ctx.workdir, "writeback.json")))
        with open(elsewhere) as f:
            self.assertEqual(json.load(f), {SID: ["comment", "move:in_review"]})

    def test_ledger_write_failure_leaves_no_temp(self):
        ctx = self.ctx()
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            err = writeback._step(ctx, "comment", lambda: None)
        self.assertIsInstance(err, OSError)
        self.assertEqual(os.listdir(ctx.workdir), [])
