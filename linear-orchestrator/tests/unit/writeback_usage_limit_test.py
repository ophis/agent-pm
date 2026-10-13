from dataclasses import replace

from agent_pm.linear_orchestrator.engine.types import LinearError, Stopped
from agent_pm.linear_orchestrator.linear import writeback
from orchestrator_test_helpers.unit.run_fixtures import logged
from orchestrator_test_helpers.unit.writeback.data import (
    HUMANS,
    ID,
    SID,
    STATES,
    T,
    move,
    post,
    reread,
    sub,
)
from orchestrator_test_helpers.unit.writeback.gql_fixtures import Gql
from orchestrator_test_helpers.unit.writeback.usage_limit_fixtures import (
    UsageLimit as UsageLimitBase,
)


class UsageLimit1(UsageLimitBase):
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
                "Usage limit, reset time unknown."
                "\n\nCould not subscribe ann@example.com: LinearError: issueSubscribe: success: false",
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
                gql = Gql(fail=lambda n, _v: err if n == name else None)
                self.assertFalse(writeback.limit(self.ctx(gql=gql), T))
                self.assertEqual(gql.calls[-1][0], name)
                self.assertEqual(self.lines()[-1], f"step-error {ID} {line}")
