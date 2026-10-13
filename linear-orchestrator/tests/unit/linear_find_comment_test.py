from agent_pm.linear_orchestrator.engine.types import LinearError, Stopped
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.data import ISSUE, comment_gql, node
from orchestrator_test_helpers.unit.linear.find_comment_fixtures import (
    FindComment as FindCommentBase,
)


class FindComment1(FindCommentBase):
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
