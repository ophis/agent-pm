"""Client boundary behavior."""

import http.client
import json
import urllib.request
from types import SimpleNamespace
from unittest import mock

from agent_pm.linear_orchestrator.engine import client, ops, types
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.utils import one_line
from orchestrator_tests.case import Case
from orchestrator_tests.unit.board_ids import TEAM
from orchestrator_tests.unit.linear_fixtures import (
    SECRET,
    VIEWER,
    errors,
    http_error,
    refused,
    respond,
)
from orchestrator_tests.unit.outage import FAILURES, failing


class LinearGql(Case):
    def gql(self, query=VIEWER, body=b'{"data": {}}', **kw):
        run = mock.Mock(return_value=SimpleNamespace(stdout="secret\n"))
        with (
            mock.patch("subprocess.run", run),
            mock.patch.object(
                urllib.request, "build_opener", side_effect=AssertionError("opener built")
            ),
            mock.patch.object(urllib.request, "urlopen", return_value=respond(body)) as urlopen,
        ):
            return client.linear_gql(query, **kw), run, urlopen

    def test_key_by_service_only_sent_by_urlopen(self):
        for service in ("svc-h", "linear-api-key-pm"):
            with self.subTest(service=service):
                out, run, urlopen = self.gql(
                    body=b'{"data": {"viewer": {"id": "v"}}}', service=service
                )
                self.assertEqual(out, {"viewer": {"id": "v"}})
                self.assertEqual(
                    [c.args[0] for c in run.call_args_list],
                    [["/usr/bin/security", "find-generic-password", "-s", service, "-w"]],
                )
                urlopen.assert_called_once()
                req = urlopen.call_args[0][0]
                self.assertEqual(
                    (req.full_url, req.get_header("Authorization")),
                    ("https://api.linear.app/graphql", "secret"),
                )
                self.assertEqual(json.loads(req.data)["variables"], {})

    def test_timeout_bounds_keychain_and_request(self):
        for kw, want in (({}, 30), ({"timeout": 5}, 5)):
            with self.subTest(timeout=want):
                _, run, urlopen = self.gql(
                    "query($i: String!) { issue(id: $i) { id } }", i="TASK-1", service="svc", **kw
                )
                self.assertEqual(run.call_args.kwargs["timeout"], want)
                self.assertEqual(urlopen.call_args.kwargs["timeout"], want)
                self.assertEqual(
                    json.loads(urlopen.call_args[0][0].data)["variables"], {"i": "TASK-1"}
                )

    def test_the_service_is_required(self):
        with (
            mock.patch("subprocess.run", side_effect=AssertionError("Keychain read")),
            mock.patch.object(
                urllib.request, "urlopen", side_effect=AssertionError("request sent")
            ),
            self.assertRaises(TypeError),
        ):
            client.linear_gql(VIEWER)
        self.assertFalse(hasattr(client, "harness_service"))

    def test_a_bad_keychain_key_raises_naming_only_the_service(self):
        for out, key in (("\n", ""), ("lin api\n", "lin api"), ("lin\x1bapi\n", "lin\x1bapi")):
            with (
                self.subTest(key=key),
                mock.patch("subprocess.run", return_value=SimpleNamespace(stdout=out)),
            ):
                code = refused(self, lambda: client.linear_gql(VIEWER, service="svc"))
                self.assertIn("svc", code)
                if key:
                    self.assertNotIn(key, code)


class Unavailable(Case):
    def setUp(self):
        p = mock.patch("subprocess.run", return_value=SimpleNamespace(stdout=SECRET + "\n"))
        p.start()
        self.addCleanup(p.stop)

    def gql(self, query, opened=None, read=None, body=b'{"data": {}}'):
        resp = respond(body)
        if read:
            resp.__enter__.return_value = mock.Mock(read=mock.Mock(side_effect=read))
        with mock.patch.object(urllib.request, "urlopen", side_effect=opened, return_value=resp):
            return client.linear_gql(query, service="svc")

    def unavailable(self, query, **kw):
        with self.assertRaises(types.Unavailable) as cm:
            self.gql(query, **kw)
        e = cm.exception
        for text in (str(e), one_line(e)):
            self.assertNotIn(SECRET, text)
        return e.op, e.status, e.reason, str(e)

    def test_5xx_and_rate_limit_carry_the_status(self):
        cases = [
            (ops.Q_TEAM, http_error(503), ("teams", 503, None, "teams: HTTP 503")),
            (
                ops.M_STATE,
                http_error(500, b"<html>"),
                ("issueUpdate", 500, None, "issueUpdate: HTTP 500"),
            ),
            (
                ops.M_COMMENT,
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
                    self.unavailable(ops.Q_TEAM, body=body),
                    ("teams", None, "response not JSON", "teams: response not JSON"),
                )

    def test_graphql_errors_in_a_200_stay_systemexit(self):
        with self.assertRaises(LinearError) as cm:
            self.gql(VIEWER, body=errors("RATELIMITED"))
        self.assertEqual(
            str(cm.exception),
            "linear api error: [{'message': 'm', 'extensions': {'code': 'RATELIMITED'}}]",
        )

    def test_malformed_response_is_unavailable(self):
        for body in (b"null", b"[]", b"1", b"{}", b'{"data": null}', b'{"data": []}'):
            with self.subTest(body=body), self.assertRaises(types.Unavailable):
                self.gql(VIEWER, body=body)

    def test_malformed_rate_limit_errors_keep_the_original_http_error(self):
        for body in (
            b"null",
            b"[]",
            b'{"errors": null}',
            b'{"errors": [null, 1, {"extensions": 1}]}',
        ):
            error = http_error(400, body)
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as caught:
                self.gql(VIEWER, opened=error)
            self.assertIs(caught.exception, error)

    def test_operation_is_the_first_field_of_the_top_level_selection_set(self):
        cases = {
            VIEWER: "viewer",
            ops.Q_TEAM: "teams",
            ops.Q_ISSUE_STATE: "issue",
            ops.M_STATE: "issueUpdate",
            ops.M_COMMENT: "commentCreate",
            ops.M_COMMENT_ID: "commentCreate",
            ops.Q_COMMENT: "comment",
            "{issues(first: 1) { nodes { id } } }": "issues",
            "query { }": "?",
            "query { 1x }": "?",
            "query": "?",
            "": "?",
        }
        for query, op in cases.items():
            with self.subTest(query=query):
                self.assertEqual(client.operation(query), op)


class Outage(Case):
    def test_each_failure_is_unavailable_and_recorded(self):
        want = {
            "5xx": (503, None),
            "timeout": (None, "TimeoutError: timed out"),
            "URLError": (None, "ConnectionRefusedError: [Errno 61] Connection refused"),
        }
        self.assertEqual(set(FAILURES), set(want))
        for name, error in FAILURES.items():
            with self.subTest(name):
                gql = failing(error)
                for _ in range(2):
                    with self.assertRaises(types.Unavailable) as cm:
                        gql(ops.Q_TEAM, t=TEAM)
                    self.assertEqual(
                        (cm.exception.op, cm.exception.status, cm.exception.reason),
                        ("teams", *want[name]),
                    )
                self.assertEqual(gql.failed, [(ops.Q_TEAM, {"t": TEAM})] * 2)

    def test_ops_names_the_failing_operations(self):
        gql = failing(
            FAILURES["5xx"], gql=lambda q, **v: {"q": q, **v}, ops={"teams", "issueUpdate"}
        )
        self.assertEqual(gql(ops.Q_ISSUE_STATE, i="I"), {"q": ops.Q_ISSUE_STATE, "i": "I"})
        for query in (ops.Q_TEAM, ops.M_STATE):
            with self.assertRaises(types.Unavailable):
                gql(query, i="I")
        self.assertEqual(gql.failed, [(ops.Q_TEAM, {"i": "I"}), (ops.M_STATE, {"i": "I"})])


class HasKey(Case):
    def test_key_existence_check_never_reads_the_secret(self):
        for code, want in ((0, True), (44, False)):
            with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=code)) as m:
                self.assertIs(client.has_key("svc"), want)
            self.assertEqual(
                m.call_args.args[0], ["/usr/bin/security", "find-generic-password", "-s", "svc"]
            )
