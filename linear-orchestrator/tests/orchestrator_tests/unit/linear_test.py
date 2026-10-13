import http.client
import json
import re
import subprocess
import urllib.request
from types import SimpleNamespace
from unittest import mock

from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_tests.case import Case
from orchestrator_tests.unit.board_ids import ACCOUNTS, STATES, TASK_GROUP, TEAM, team_node
from orchestrator_tests.unit.linear_data import (
    LABEL1,
    LABEL2,
    OTHER,
    SECRET,
    VIEWER,
    errors,
    http_error,
    refused,
    respond,
)
from orchestrator_tests.unit.outage import FAILURES, failing


class RoleIds(Case):
    def test_role_ids(self):
        roles = {r: SimpleNamespace(account=a) for r, a in ACCOUNTS.items()}

        def gql(_query, **variables):
            return {"users": {"nodes": [{"id": "u-" + variables["e"]}]}}

        self.assertEqual(linear.role_ids(gql, roles), {f"u-{a}": r for r, a in ACCOUNTS.items()})
        with self.assertRaises(LinearError) as cm:
            linear.role_ids(
                lambda _q, **_v: {"users": {"nodes": []}}, {"engineer": roles["engineer"]}
            )
        self.assertEqual(
            str(cm.exception),
            (
                "linear-orchestrator/config.toml [roles.engineer]: account "
                "'engineer@agents.test' not found in Linear"
            ),
        )


class Humans(Case):
    def test_humans(self):
        def gql(_q, **v):
            return {"users": {"nodes": [{"id": "u-" + v["e"]}] if v["e"] != "x@y.z" else []}}

        self.assertEqual(linear.humans(gql, SimpleNamespace(human_members=("a@b.c",))), ["u-a@b.c"])
        self.assertEqual(linear.humans(gql, SimpleNamespace(human_members=())), [])
        with self.assertRaises(LinearError) as cm:
            linear.humans(gql, SimpleNamespace(human_members=("a@b.c", "x@y.z")))
        self.assertEqual(
            str(cm.exception),
            "linear-orchestrator/config.toml: human_members not found in Linear: x@y.z",
        )


class TeamCheck(Case):
    def cfg(self):
        return SimpleNamespace(team=TEAM, states=dict(STATES))

    def gql(self, nodes):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            return {"teams": {"nodes": nodes}}

        gql.calls = calls
        return gql

    def test_ok(self):
        gql = self.gql([team_node()])
        self.assertEqual(linear.team(gql, self.cfg()), linear.Team(TEAM, "Team", dict(STATES)))
        self.assertEqual(gql.calls, [(linear.Q_TEAM, {"t": TEAM})])

    def test_team_not_found_or_states_outside_team(self):
        other = [i for k, i in STATES.items() if k not in ("handoff", "done")]
        cases = {
            f"team {TEAM} not found in Linear": [],
            (
                f"[states] not workflow states of team 'Team': handoff {STATES['handoff']}"
                f", done {STATES['done']}"
            ): [team_node(other)],
        }
        for message, nodes in cases.items():
            with self.subTest(message):
                with self.assertRaises(LinearError) as cm:
                    linear.team(self.gql(nodes), self.cfg())
                self.assertEqual(
                    str(str(cm.exception)), "linear-orchestrator/config.toml: " + message
                )


class TaskGroupCheck(Case):
    LABELS = {"light-research": LABEL1, "deep-research": LABEL2}

    def cfg(self, labels=LABELS):
        return SimpleNamespace(task_label_group=TASK_GROUP, task_labels=labels)

    def gql(self, node=None, error=None):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            if error:
                raise LinearError(error)
            return {"issueLabel": node}

        gql.calls = calls
        return gql

    def group(self, *ids):
        return {"isGroup": True, "children": {"nodes": [{"id": i} for i in ids]}}

    def test_group_ok(self):
        for group, labels in ((self.group(LABEL1, LABEL2, OTHER), self.LABELS), (self.group(), {})):
            with self.subTest(labels=labels):
                gql = self.gql(group)
                self.assertIsNone(linear.task_group(gql, self.cfg(labels)))
                self.assertEqual(gql.calls, [(linear.Q_TASK_GROUP, {"i": TASK_GROUP})])
        self.assertIn(
            "issueLabel(id: $i) { isGroup children(first: 250) { nodes { id } } }",
            linear.Q_TASK_GROUP,
        )

    def test_fails(self):
        error = "linear api error: [{'message': 'Entity not found: IssueLabel'}]"
        not_labels = f"[task_labels] not labels of task_label_group {TASK_GROUP}: "
        cases = [
            (self.gql(self.group(LABEL1)), self.LABELS, not_labels + f"deep-research {LABEL2}"),
            (
                self.gql(self.group(OTHER)),
                {"deep-research": LABEL2, "light-research": LABEL1, "ok": OTHER},
                not_labels + f"deep-research {LABEL2}, light-research {LABEL1}",
            ),
            (
                self.gql(error=error),
                self.LABELS,
                f"task_label_group {TASK_GROUP} not found in Linear: {error}",
            ),
            (
                self.gql(
                    {"isGroup": False, "children": {"nodes": [{"id": LABEL1}, {"id": LABEL2}]}}
                ),
                self.LABELS,
                f"task_label_group {TASK_GROUP} is not a label group",
            ),
        ]
        for gql, labels, message in cases:
            with self.subTest(message):
                with self.assertRaises(LinearError) as cm:
                    linear.task_group(gql, self.cfg(labels))
                self.assertEqual(
                    str(str(cm.exception)), "linear-orchestrator/config.toml: " + message
                )
                self.assertEqual(len(gql.calls), 1)


class LinearGql(Case):
    def gql(self, query=VIEWER, body=b'{"data": {}}', **kw):
        run = mock.Mock(return_value=SimpleNamespace(stdout="secret\n"))
        with (
            mock.patch.object(subprocess, "run", run),
            mock.patch.object(
                urllib.request, "build_opener", side_effect=AssertionError("opener built")
            ),
            mock.patch.object(urllib.request, "urlopen", return_value=respond(body)) as urlopen,
        ):
            return linear.linear_gql(query, **kw), run, urlopen

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
            mock.patch.object(subprocess, "run", side_effect=AssertionError("Keychain read")),
            mock.patch.object(
                urllib.request, "urlopen", side_effect=AssertionError("request sent")
            ),
            self.assertRaises(TypeError),
        ):
            linear.linear_gql(VIEWER)
        self.assertFalse(hasattr(linear, "harness_service"))

    def test_a_bad_keychain_key_raises_naming_only_the_service(self):
        for out, key in (("\n", ""), ("lin api\n", "lin api"), ("lin\x1bapi\n", "lin\x1bapi")):
            with (
                self.subTest(key=key),
                mock.patch.object(subprocess, "run", return_value=SimpleNamespace(stdout=out)),
            ):
                code = refused(self, lambda: linear.linear_gql(VIEWER, service="svc"))
                self.assertIn("svc", code)
                if key:
                    self.assertNotIn(key, code)


class Unavailable(Case):
    def setUp(self):
        p = mock.patch.object(subprocess, "run", return_value=SimpleNamespace(stdout=SECRET + "\n"))
        p.start()
        self.addCleanup(p.stop)

    def gql(self, query, opened=None, read=None, body=b'{"data": {}}'):
        resp = respond(body)
        if read:
            resp.__enter__.return_value = mock.Mock(read=mock.Mock(side_effect=read))
        with mock.patch.object(urllib.request, "urlopen", side_effect=opened, return_value=resp):
            return linear.linear_gql(query, service="svc")

    def unavailable(self, query, **kw):
        with self.assertRaises(linear.Unavailable) as cm:
            self.gql(query, **kw)
        e = cm.exception
        for text in (str(e), linear.one_line(e)):
            self.assertNotIn(SECRET, text)
        return e.op, e.status, e.reason, str(e)

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
                    with self.assertRaises(linear.Unavailable) as cm:
                        gql(linear.Q_TEAM, t=TEAM)
                    self.assertEqual(
                        (cm.exception.op, cm.exception.status, cm.exception.reason),
                        ("teams", *want[name]),
                    )
                self.assertEqual(gql.failed, [(linear.Q_TEAM, {"t": TEAM})] * 2)

    def test_ops_names_the_failing_operations(self):
        gql = failing(
            FAILURES["5xx"], gql=lambda q, **v: {"q": q, **v}, ops={"teams", "issueUpdate"}
        )
        self.assertEqual(gql(linear.Q_ISSUE_STATE, i="I"), {"q": linear.Q_ISSUE_STATE, "i": "I"})
        for query in (linear.Q_TEAM, linear.M_STATE):
            with self.assertRaises(linear.Unavailable):
                gql(query, i="I")
        self.assertEqual(gql.failed, [(linear.Q_TEAM, {"i": "I"}), (linear.M_STATE, {"i": "I"})])


class HasKey(Case):
    def test_key_existence_check_never_reads_the_secret(self):
        for code, want in ((0, True), (44, False)):
            with mock.patch.object(
                subprocess, "run", return_value=SimpleNamespace(returncode=code)
            ) as m:
                self.assertIs(linear.has_key("svc"), want)
            self.assertEqual(
                m.call_args.args[0], ["/usr/bin/security", "find-generic-password", "-s", "svc"]
            )


class Helpers(Case):
    def test_issue_id_is_unanchored(self):
        self.assertEqual(re.findall(linear.ISSUE_ID, "see ENG-12, PM-3."), ["ENG-12", "PM-3"])
        for bad in ("task-7", "7-ENG", "ENG-", "ENG-7 "):
            self.assertIsNone(re.fullmatch(linear.ISSUE_ID, bad), bad)

    def test_one_line(self):
        self.assertEqual(linear.one_line("a\n\n  b\tc "), "a b c")
        self.assertEqual(linear.one_line(RuntimeError("two\nlines")), "RuntimeError: two lines")
        self.assertEqual(linear.one_line(LinearError("api error")), "LinearError: api error")

    def test_stamp_is_local_time_in_seconds(self):
        self.assertRegex(linear.stamp(), r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d\Z")
