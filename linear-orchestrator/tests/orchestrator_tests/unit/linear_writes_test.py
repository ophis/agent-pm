from agent_pm.linear_orchestrator.engine.types import LinearError, Stopped
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_tests.case import Case
from orchestrator_tests.unit.board_ids import STATES
from orchestrator_tests.unit.linear_data import (
    CHILD,
    IN_PROGRESS,
    IN_REVIEW,
    ISSUE,
    TODO,
    comment_gql,
    node,
)
from orchestrator_tests.unit.linear_fakes import Gql


class Writes(Case):
    def test_queries_verbatim(self):
        self.assertEqual(
            linear.M_SUBSCRIBE,
            (
                "mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail:"
                " $e) { success } }"
            ),
        )
        self.assertEqual(
            linear.M_COMMENT,
            (
                "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: "
                "$i, body: $b }) { success } }"
            ),
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
            (
                "mutation($i: String!, $s: String!) { issueUpdate(id: $i, input: { "
                "stateId: $s }) { success } }"
            ),
        )
        self.assertEqual(
            linear.Q_ISSUE_STATE, "query($i: String!) { issue(id: $i) { state { id } } }"
        )
        self.assertEqual(
            linear.HISTORY,
            (
                "history(first: 250, orderBy: createdAt) { nodes { createdAt actorId "
                "fromStateId toStateId } }"
            ),
        )

    def test_call_returns_the_field_and_raises_on_success_false(self):
        gql = Gql()
        self.assertEqual(
            linear.call(gql, linear.M_COMMENT, "commentCreate", i="I", b="x"), {"success": True}
        )
        self.assertEqual(gql.calls, [("comment", {"i": "I", "b": "x"})])
        with self.assertRaises(LinearError) as cm:
            linear.call(
                Gql(fail=lambda name, v: False), linear.M_COMMENT, "commentCreate", i="I", b="x"
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
            linear.comment(Gql(fail=lambda name, v: False), "I", "y", cid="c-1")

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
            fail=lambda name, v: LinearError("linear api error: boom") if name == "move" else None
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
        gql = Gql(fail=lambda name, v: Stopped(130) if name == "subscribe" else None)
        with self.assertRaises(Stopped) as cm:
            linear.comment_and_move(
                gql, "I", "body", IN_REVIEW, IN_PROGRESS, ("a@x.com", "b@x.com")
            )
        self.assertEqual(cm.exception.code, 130)
        self.assertEqual(gql.calls, [("subscribe", {"i": "I", "e": "a@x.com"})])


class ClientId(Case):
    def test_promotes_child_id_values(self):
        self.assertEqual(linear.client_id("DR-1", "pm", "2026-09-27T10:00:00.000Z"), CHILD)
        self.assertNotEqual(linear.client_id("DR-1", "pm", "2026-09-27T10:00:01.000Z"), CHILD)
        self.assertNotEqual(linear.client_id("DR-1", "engineer", "2026-09-27T10:00:00.000Z"), CHILD)

    def test_stable_and_shaped_as_uuid_v4(self):
        sid = "0b6f2c1e-6d0a-4c1b-9a51-3f1f6b0e2a7d"
        a = linear.client_id(sid, "session")
        self.assertEqual(a, linear.client_id(sid, "session"))
        self.assertEqual(a, "6aa10f29-00e8-42bb-88f1-e74018389257")
        self.assertNotEqual(a, linear.client_id(sid, "start"))
        self.assertEqual(linear.client_id(sid, "start"), linear.client_id(f"{sid}/start"))
        for cid in (a, CHILD):
            self.assertRegex(
                cid, r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
            )


class FindComment(Case):
    def test_found(self):
        gql = comment_gql(node("Progress (x): y"))
        self.assertEqual(linear.find_comment(gql, "c-1", ISSUE), "Progress (x): y")
        self.assertEqual(gql.calls, [(linear.Q_COMMENT, {"c": "c-1"})])

    def test_not_found(self):
        for gql in (
            comment_gql(),
            comment_gql(
                error=LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")
            ),
        ):
            with self.subTest(calls=gql):
                self.assertIsNone(linear.find_comment(gql, "c-1", ISSUE))

    def test_other_errors_raise(self):
        for error in (
            LinearError("linear api error: boom"),
            Stopped(130),
            linear.Unavailable("comment", status=503),
            RuntimeError("x"),
        ):
            with self.subTest(error=repr(error)), self.assertRaises(type(error)) as cm:
                linear.find_comment(comment_gql(error=error), "c-1", ISSUE)
            self.assertIs(cm.exception, error)

    def test_another_issues_or_authors_comment_is_not_ours(self):
        for found in (
            node(issue="other"),
            node(me=False),
            {**node(), "user": None},
            {**node(), "issue": None},
        ):
            with self.subTest(found=found), self.assertRaises(LinearError) as cm:
                linear.find_comment(comment_gql(found), "c-1", ISSUE)
            self.assertEqual(str(cm.exception), "comment c-1 is not ours")

    def test_not_found_names_only_that_text(self):
        self.assertTrue(
            linear.not_found(
                LinearError("linear api error: [{'message': 'Entity not found: Comment'}]")
            )
        )
        for e in (
            LinearError("linear api error: boom"),
            Stopped(130),
            RuntimeError("Entity not found"),
        ):
            with self.subTest(e=repr(e)):
                self.assertFalse(linear.not_found(e))


class LastMove(Case):
    NODES = [
        {"createdAt": "2026-09-27T10:00:00.000Z", "actorId": "human", "toStateId": TODO},
        {"createdAt": "2026-09-27T12:00:00.000Z", "actorId": "agent", "toStateId": TODO},
        {"createdAt": "2026-09-27T11:00:00.000Z", "actorId": "agent", "toStateId": IN_REVIEW},
        {"createdAt": "2026-09-27T13:00:00.000Z", "actorId": None, "toStateId": IN_PROGRESS},
    ]

    def test_latest_move_into_the_states_by_actors(self):
        at = linear.parse_time
        for nodes, states, actors, want in (
            (self.NODES, {TODO}, None, at("2026-09-27T12:00:00.000Z")),
            (self.NODES, {TODO, IN_REVIEW}, None, at("2026-09-27T12:00:00.000Z")),
            (self.NODES, {IN_REVIEW}, None, at("2026-09-27T11:00:00.000Z")),
            (self.NODES, {STATES["done"]}, None, None),
            ([], {TODO}, None, None),
            (self.NODES, {TODO}, {"human"}, at("2026-09-27T10:00:00.000Z")),
            (self.NODES, {IN_PROGRESS}, {"human", "agent"}, None),
            (self.NODES, {TODO}, set(), None),
        ):
            with self.subTest(states=states, actors=actors, nodes=len(nodes)):
                self.assertEqual(linear.last_move(nodes, states, actors), want)
