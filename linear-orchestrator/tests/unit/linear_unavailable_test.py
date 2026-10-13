import http.client
import urllib.request

from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.data import VIEWER, errors, http_error
from orchestrator_test_helpers.unit.linear.unavailable_fixtures import (
    Unavailable as UnavailableBase,
)


class Unavailable1(UnavailableBase):
    def test_5xx_and_rate_limit_carry_the_status(self):
        cases = [
            (linear.Q_TEAM, http_error(503), ("teams", 503, None, "teams: HTTP 503")),
            (
                linear.M_STATE,
                http_error(500, b"<html>"),
                ("issueUpdate", 500, None, "issueUpdate: HTTP 500"),
            ),
            (
                linear.M_COMMENT,
                http_error(400, errors("OTHER", "RATELIMITED")),
                ("commentCreate", 400, "RATELIMITED", "commentCreate: RATELIMITED"),
            ),
        ]
        for query, error, want in cases:
            with self.subTest(want=want):
                self.assertEqual(self.unavailable(query, opened=error), want)

    def test_other_http_errors_unchanged(self):
        for error in (
            http_error(400, errors("GRAPHQL_VALIDATION_FAILED")),
            http_error(400, b"not json"),
            http_error(400),
            http_error(400, b'{"errors": 1}'),
            http_error(401, errors("AUTHENTICATION_ERROR")),
            http_error(401, errors("RATELIMITED")),
        ):
            with (
                self.subTest(code=error.code),
                error,
                self.assertRaises(urllib.error.HTTPError) as cm,
            ):
                self.gql(VIEWER, opened=error)
            self.assertIs(cm.exception, error)

    def test_transport_errors_carry_the_reason(self):
        cases = [
            ({"opened": TimeoutError("timed out")}, "TimeoutError: timed out"),
            (
                {"opened": urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))},
                "ConnectionRefusedError: [Errno 61] Connection refused",
            ),
            ({"opened": urllib.error.URLError("no\n host")}, "no host"),
            (
                {"opened": ConnectionResetError(54, "Connection reset by peer")},
                "ConnectionResetError: [Errno 54] Connection reset by peer",
            ),
            (
                {"read": http.client.IncompleteRead(b"{", 9)},
                "IncompleteRead: IncompleteRead(1 bytes read, 9 more expected)",
            ),
            (
                {"read": TimeoutError("The read operation timed out")},
                "TimeoutError: The read operation timed out",
            ),
        ]
        for kw, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.unavailable(VIEWER, **kw), ("viewer", None, reason, f"viewer: {reason}")
                )

    def test_a_2xx_body_not_json(self):
        for body in (b"<html>502 Bad Gateway</html>", b"", b"\xff\xfe{"):
            with self.subTest(body=body):
                self.assertEqual(
                    self.unavailable(linear.Q_TEAM, body=body),
                    ("teams", None, "response not JSON", "teams: response not JSON"),
                )

    def test_graphql_errors_in_a_200_stay_systemexit(self):
        with self.assertRaises(LinearError) as cm:
            self.gql(VIEWER, body=errors("RATELIMITED"))
        self.assertEqual(
            str(cm.exception),
            "linear api error: [{'message': 'm', 'extensions': {'code': 'RATELIMITED'}}]",
        )

    def test_operation_is_the_first_field_of_the_top_level_selection_set(self):
        cases = {
            VIEWER: "viewer",
            linear.Q_TEAM: "teams",
            linear.Q_ISSUE_STATE: "issue",
            linear.M_STATE: "issueUpdate",
            linear.M_COMMENT: "commentCreate",
            linear.M_COMMENT_ID: "commentCreate",
            linear.Q_COMMENT: "comment",
            "{issues(first: 1) { nodes { id } } }": "issues",
            "query { }": "?",
            "query { 1x }": "?",
            "query": "?",
            "": "?",
        }
        for query, op in cases.items():
            with self.subTest(query=query):
                self.assertEqual(linear.operation(query), op)
