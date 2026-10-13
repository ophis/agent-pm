from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit.writeback.data import (
    HUMANS,
    ID,
    SID,
    SPEC_TEXT,
    STATES,
    expect,
    move,
    outcome,
    plan_comment,
    reread,
    sha,
    spec_comment,
)
from orchestrator_test_helpers.unit.writeback.finish_steps_fixtures import (
    FinishSteps as FinishStepsBase,
)
from orchestrator_test_helpers.unit.writeback.gql_fixtures import Gql
from orchestrator_test_helpers.unit.writeback_support import PR


class FinishSteps1(FinishStepsBase):
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
                gql = Gql(fail=lambda name, _v: err if name == "attach" else None)
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
            f"step-error {ID} step=file:ENG-7-spec.md:{sha(SPEC_TEXT)} error=LinearError: linear api error: body too long",
            self.lines(),
        )
