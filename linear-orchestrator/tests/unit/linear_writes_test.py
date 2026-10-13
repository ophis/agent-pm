from agent_pm.linear_orchestrator.engine.types import LinearError, Stopped
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.data import IN_PROGRESS, IN_REVIEW, TODO
from orchestrator_test_helpers.unit.linear.gql_fixtures import Gql
from orchestrator_test_helpers.unit.linear.writes_fixtures import Writes as WritesBase


class Writes1(WritesBase):
    def test_queries_verbatim(self):
        self.assertEqual(
            linear.M_SUBSCRIBE,
            "mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail: $e) { success } }",
        )
        self.assertEqual(
            linear.M_COMMENT,
            "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }",
        )
        self.assertEqual(
            linear.M_COMMENT_ID,
            "mutation($c: String!, $i: String!, $b: String!) { commentCreate(input: { id: $c, "
            "issueId: $i, body: $b }) { success } }",
        )
        self.assertEqual(
            linear.Q_COMMENT,
            "query($c: String!) { comment(id: $c) { id body issue { id } user { isMe } } }",
        )
        self.assertEqual(linear.NOT_FOUND, "Entity not found")
        self.assertEqual(
            linear.M_STATE,
            "mutation($i: String!, $s: String!) { issueUpdate(id: $i, input: { stateId: $s }) { success } }",
        )
        self.assertEqual(
            linear.Q_ISSUE_STATE, "query($i: String!) { issue(id: $i) { state { id } } }"
        )
        self.assertEqual(
            linear.HISTORY,
            "history(first: 250, orderBy: createdAt) { nodes { createdAt actorId fromStateId toStateId } }",
        )

    def test_call_returns_the_field_and_raises_on_success_false(self):
        gql = Gql()
        self.assertEqual(
            linear.call(gql, linear.M_COMMENT, "commentCreate", i="I", b="x"), {"success": True}
        )
        self.assertEqual(gql.calls, [("comment", {"i": "I", "b": "x"})])
        with self.assertRaises(LinearError) as cm:
            linear.call(
                Gql(fail=lambda _name, _v: False), linear.M_COMMENT, "commentCreate", i="I", b="x"
            )
        self.assertEqual(str(cm.exception), "commentCreate: success: false")

    def test_comment_with_and_without_a_client_id(self):
        gql = Gql()
        linear.comment(gql, "I", "x")
        linear.comment(gql, "I", "y", "c-1")
        self.assertEqual(
            gql.calls,
            [("comment", {"i": "I", "b": "x"}), ("comment_id", {"c": "c-1", "i": "I", "b": "y"})],
        )
        with self.assertRaises(LinearError):
            linear.comment(Gql(fail=lambda _name, _v: False), "I", "y", cid="c-1")

    def test_move(self):
        for state, want, calls in (
            (IN_PROGRESS, None, [("read", {"i": "I"}), ("move", {"i": "I", "s": IN_REVIEW})]),
            (IN_REVIEW, IN_REVIEW, [("read", {"i": "I"})]),
            (TODO, TODO, [("read", {"i": "I"})]),
        ):
            with self.subTest(state=state):
                gql = Gql(state)
                self.assertEqual(linear.move(gql, "I", IN_REVIEW, IN_PROGRESS), want)
                self.assertEqual(gql.calls, calls)

    def test_comment_and_move_order(self):
        gql = Gql()
        self.assertIsNone(
            linear.comment_and_move(
                gql, "I", "body", IN_REVIEW, IN_PROGRESS, ("a@x.com", "b@x.com")
            )
        )
        self.assertEqual(
            gql.calls,
            [
                ("subscribe", {"i": "I", "e": "a@x.com"}),
                ("subscribe", {"i": "I", "e": "b@x.com"}),
                ("read", {"i": "I"}),
                ("move", {"i": "I", "s": IN_REVIEW}),
                ("comment", {"i": "I", "b": "body"}),
            ],
        )
        gql = Gql()
        linear.comment_and_move(gql, "I", "body", TODO, IN_PROGRESS)
        self.assertEqual([n for n, _ in gql.calls], ["read", "move", "comment"])

    def test_skipped_or_failed_move_posts_no_comment(self):
        for state in (IN_REVIEW, TODO):
            with self.subTest(state=state):
                gql = Gql(state)
                self.assertEqual(
                    linear.comment_and_move(gql, "I", "body", IN_REVIEW, IN_PROGRESS, ("a@x.com",)),
                    state,
                )
                self.assertEqual([n for n, _ in gql.calls], ["subscribe", "read"])
        gql = Gql(
            fail=lambda name, _v: LinearError("linear api error: boom") if name == "move" else None
        )
        with self.assertRaises(LinearError):
            linear.comment_and_move(gql, "I", "body", IN_REVIEW, IN_PROGRESS)
        self.assertEqual([n for n, _ in gql.calls], ["read", "move"])

    def test_subscribe_failures_noted_in_the_comment(self):
        def fail(name, v):
            if name == "subscribe":
                return False if v["e"] == "a@x.com" else LinearError("linear api error: boom")

        gql = Gql(fail=fail)
        self.assertIsNone(
            linear.comment_and_move(
                gql, "I", "body", IN_REVIEW, IN_PROGRESS, ("a@x.com", "b@x.com")
            )
        )
        self.assertEqual(
            gql.calls[-1],
            (
                "comment",
                {
                    "i": "I",
                    "b": "body"
                    "\n\nCould not subscribe a@x.com: LinearError: issueSubscribe: success: false"
                    "\n\nCould not subscribe b@x.com: LinearError: linear api error: boom",
                },
            ),
        )
        self.assertEqual(linear.subscribe(Gql(), "I", ("a@x.com",)), "")

    def test_signal_exit_reraised(self):
        gql = Gql(fail=lambda name, _v: Stopped(130) if name == "subscribe" else None)
        with self.assertRaises(Stopped) as cm:
            linear.comment_and_move(
                gql, "I", "body", IN_REVIEW, IN_PROGRESS, ("a@x.com", "b@x.com")
            )
        self.assertEqual(cm.exception.code, 130)
        self.assertEqual(gql.calls, [("subscribe", {"i": "I", "e": "a@x.com"})])
