import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from agent_pm.linear_orchestrator.engine.types import ConfigError, LinearError, Stopped
from agent_pm.linear_orchestrator.linear import linear
from orchestrator_tests.case import Case
from orchestrator_tests.unit.board_ids import ACCOUNTS, STATES, TASK_GROUP, TEAM, team_node
from orchestrator_tests.unit.clock import frozen, logs_at
from orchestrator_tests.unit.outage import FAILURES, failing
from orchestrator_tests.unit.streams import Stderr

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"

OTHER = "00000000-0000-4000-8000-000000000023"

SECRET = "lin_api_SECRET"

VIEWER = "query { viewer { id } }"


def respond(body=b'{"data": {}}'):
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(body)
    return resp


def refused(case, call):
    with (
        mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("request sent")),
        mock.patch.object(
            urllib.request, "build_opener", side_effect=AssertionError("request sent")
        ),
        case.assertRaises(LinearError) as cm,
    ):
        call()
    case.assertIsInstance(str(cm.exception), str)
    return str(cm.exception)


def http_error(code, body=b""):
    return urllib.error.HTTPError(linear.URL, code, "Error", {}, io.BytesIO(body))


def errors(*codes):
    return json.dumps(
        {"errors": [{"message": "m", "extensions": {"code": c}} for c in codes]}
    ).encode()


IN_PROGRESS, IN_REVIEW, TODO = STATES["in_progress"], STATES["in_review"], STATES["todo"]

NAMES = {
    linear.M_SUBSCRIBE: "subscribe",
    linear.Q_ISSUE_STATE: "read",
    linear.M_STATE: "move",
    linear.M_COMMENT: "comment",
    linear.M_COMMENT_ID: "comment_id",
}

FIELDS = {
    "subscribe": "issueSubscribe",
    "move": "issueUpdate",
    "comment": "commentCreate",
    "comment_id": "commentCreate",
}

CHILD = "b52be5d1-f8aa-436f-af99-3e185bd14ef5"

ISSUE = "issue-uuid"


def comment_gql(node=None, error=None):
    calls = []

    def gql(query, **v):
        calls.append((query, v))
        if error is not None:
            raise error
        return {"comment": node}

    gql.calls = calls
    return gql


def node(body="Run x", issue=ISSUE, *, me=True):
    return {"id": "c-1", "body": body, "issue": {"id": issue}, "user": {"isMe": me}}


class Gql:
    def __init__(self, state=IN_PROGRESS, fail=lambda _name, _v: None):
        self.state, self.fail, self.calls = state, fail, []

    def __call__(self, query, **v):
        name = NAMES[query]
        self.calls.append((name, v))
        r = self.fail(name, v)
        if isinstance(r, BaseException):
            raise r
        if name == "read":
            return {"issue": {"state": {"id": self.state}}}
        return {FIELDS[name]: {"success": r is not False}}


class Logs(Case):
    TS = "2026-10-09T00:26:33-04:00"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.logs = os.path.join(self.tmp, "logs")
        self.path = os.path.join(self.logs, "orchestrator.jsonl")
        linear.use_logs(self.logs)

    def stderr(self, call, fail=None, *, tty=False):
        err = Stderr(tty=tty, fail=fail)
        with (
            mock.patch.object(sys, "stderr", err),
            frozen(self.TS),
        ):
            out = call()
        return out, err.getvalue()

    def text(self, path=None):
        try:
            with open(path or self.path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def lines(self):
        return [json.loads(line) for line in self.text().splitlines()]

    def json(self, src, kind, issue=None, **fields):
        line = {
            "ts": self.TS,
            "src": src,
            "kind": kind,
            **({"issue": issue} if issue else {}),
            **fields,
        }
        return json.dumps(line, ensure_ascii=False) + "\n"


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


class Log(Logs):
    def log(self, *args, tty=False, fail=None, **kw):
        return self.stderr(lambda: linear.log(*args, **kw), tty=tty, fail=fail)[1]

    def test_line_shape_and_key_order(self):
        self.assertEqual(self.log("router", "claim", "TASK-1", role="pm", task=None), "")
        self.log("prune", "prune-skip", entry="TASK-1/src/o/n", reason="not a clone é")
        self.assertEqual(
            self.text(),
            f'{{"ts": "{self.TS}", "src": "router", "kind": "claim", "issue": "TASK-1", '
            '"role": "pm"}\n'
            f'{{"ts": "{self.TS}", "src": "prune", "kind": "prune-skip", '
            '"entry": "TASK-1/src/o/n", "reason": "not a clone é"}\n',
        )

    def test_the_file_is_0600_in_logs_dir_resolved_per_call(self):
        self.log("run", "end", "TASK-1", sid="s", exit=0)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        other = os.path.join(self.tmp, "other")
        with logs_at(other):
            self.log("run", "end", "TASK-2", sid="s", exit=0)
        self.assertEqual([d["issue"] for d in self.lines()], ["TASK-1"])
        with open(os.path.join(other, "orchestrator.jsonl")) as f:
            self.assertEqual(json.loads(f.read())["issue"], "TASK-2")

    def test_use_logs_sets_the_dir(self):
        other = os.path.join(self.tmp, "other")
        linear.use_logs(other)
        self.assertEqual(linear.LOGS_DIR, other)
        self.log("run", "end", "TASK-2")
        self.assertTrue(os.path.isfile(os.path.join(other, "orchestrator.jsonl")))

    def test_no_logs_dir_writes_no_file(self):
        with (
            logs_at(None),
            mock.patch.object(os, "makedirs") as makedirs,
        ):
            self.assertEqual(
                self.log("run", "end", "TASK-1", once=True, tty=True),
                self.json("run", "end", "TASK-1"),
            )
            self.assertEqual(self.log("run", "end", "TASK-1"), "")
        makedirs.assert_not_called()
        self.assertEqual(os.listdir(self.tmp), [])

    def test_an_oserror_from_the_file_never_raises(self):
        os.makedirs(self.logs)
        os.symlink(os.path.join(self.tmp, "elsewhere"), self.path)
        self.assertEqual(
            self.log("run", "end", "TASK-1", tty=True), self.json("run", "end", "TASK-1")
        )
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "elsewhere")))
        file = os.path.join(self.tmp, "file")
        open(file, "w").close()
        with logs_at(os.path.join(file, "logs")):
            self.log("run", "end", "TASK-1")

    def test_an_oserror_from_stderr_never_raises(self):
        for dry in (False, True):
            self.log(
                "router",
                "skip",
                tty=True,
                dry=dry,
                fail=OSError(5, "Input/output error"),
                reason="x",
            )
        self.assertEqual(len(self.lines()), 1)

    def test_a_stderr_copy_only_on_a_terminal_or_dry_and_printable(self):
        self.assertEqual(self.log("run", "tui-error", "TASK-1", msg="a"), "")
        self.assertEqual(
            self.log("run", "tui-error", "TASK-1", tty=True, msg="a\x9b2Jb"),
            self.json("run", "tui-error", "TASK-1", msg="a2Jb"),
        )
        self.assertEqual(self.lines()[-1]["msg"], "a\x9b2Jb")
        self.assertEqual(
            self.log("router", "pick", "TASK-2", dry=True, queue=1),
            self.json("router", "pick", "TASK-2", queue=1),
        )
        self.assertEqual(len(self.lines()), 2)

    def test_once_dedups_on_src_kind_issue_entry_and_reason(self):
        base = dict(
            src="router",
            kind="usage-skip",
            issue=None,
            mode="new",
            reason="blocked by usage",
            usage="five_hour=0.95",
        )
        cases = [
            ({}, True),
            ({"usage": "five_hour=0.97", "mode": "resume"}, False),
            ({"reason": "other"}, True),
            ({"issue": "TASK-1"}, True),
            ({"src": "promote"}, True),
            ({"kind": "blocked"}, True),
            ({"entry": "TASK-1/tmp"}, True),
            ({}, False),
            ({"issue": "TASK-1"}, False),
        ]
        for change, written in cases:
            with self.subTest(change=change):
                before = len(self.lines())
                kw = {**base, **change}
                self.log(kw.pop("src"), kw.pop("kind"), kw.pop("issue"), once=True, **kw)
                self.assertEqual(len(self.lines()) - before, int(written))
        self.log("router", "usage-skip", mode="new", reason="blocked by usage", usage="x")
        self.assertEqual(len(self.lines()), 7)

    def test_once_reads_the_last_24_hours_of_the_file(self):
        now = datetime.fromisoformat(self.TS)
        recent, old = (now - timedelta(hours=h) for h in (23, 25))
        os.makedirs(self.logs)
        lines = [
            {
                "ts": recent.isoformat(),
                "src": "router",
                "kind": "blocked",
                "issue": "TASK-1",
                "by": ["TASK-7"],
            },
            {
                "ts": old.isoformat(),
                "src": "router",
                "kind": "blocked",
                "issue": "TASK-2",
                "by": ["TASK-7"],
            },
            {
                "ts": recent.isoformat(),
                "src": "prune",
                "kind": "prune-skip",
                "entry": "TASK-3/tmp",
                "reason": "r",
            },
        ]
        junk = [
            "router.py: crash",
            "[1]",
            '"x"',
            "{",
            json.dumps({"src": "router", "kind": "blocked", "issue": "TASK-4"}),
            json.dumps({"ts": "yesterday", "src": "router", "kind": "blocked", "issue": "TASK-5"}),
            json.dumps(
                {
                    "ts": "9999-12-31T23:59:59-05:00",
                    "src": "router",
                    "kind": "blocked",
                    "issue": "TASK-6",
                }
            ),
            json.dumps(
                {
                    "ts": now.replace(tzinfo=None).isoformat(),
                    "src": "router",
                    "kind": "blocked",
                    "issue": "TASK-7",
                }
            ),
        ]
        with open(self.path, "w") as f:
            f.write("".join(json.dumps(d) + "\n" for d in lines) + "".join(j + "\n" for j in junk))
        for issue in ("TASK-1", "TASK-2", "TASK-4", "TASK-5", "TASK-6", "TASK-7"):
            self.log("router", "blocked", issue, once=True, by=["TASK-8"])
        self.log("prune", "prune-skip", once=True, entry="TASK-3/tmp", reason="r")
        self.assertEqual(
            [
                json.loads(line)["issue"]
                for line in self.text().splitlines()[len(lines) + len(junk) :]
            ],
            ["TASK-2", "TASK-4", "TASK-5", "TASK-7"],
        )

    def test_a_dry_run_ignores_once(self):
        self.log("router", "blocked", "TASK-1", once=True, by=["TASK-7"])
        for _ in range(2):
            self.assertEqual(
                self.log("router", "blocked", "TASK-1", once=True, dry=True, by=["TASK-7"]),
                self.json("router", "blocked", "TASK-1", by=["TASK-7"]),
            )
        self.assertEqual([d["issue"] for d in self.lines()], ["TASK-1"])

    def test_once_keeps_a_cache_per_log_path(self):
        other = os.path.join(self.tmp, "other")
        for logs, written in ((self.logs, 1), (other, 1), (self.logs, 0), (other, 0)):
            with logs_at(logs):
                path = os.path.join(logs, "orchestrator.jsonl")
                before = len(self.text(path).splitlines())
                self.log("router", "blocked", "TASK-1", once=True, by=["TASK-7"])
                self.assertEqual(len(self.text(path).splitlines()) - before, written, logs)


class LogsDirDefault(Case):
    def test_unset_until_an_entry_sets_it(self):
        self.assertIsNone(linear.LOGS_DIR)


class ApiErrors(Logs):
    def error(self, e, src="router", *, tty=False, dry=False):
        return self.stderr(lambda: linear.linear_error(src, e, dry=dry), tty=tty)

    def test_once_a_day_per_reason_printed_only_on_a_terminal(self):
        timeout, limit = (
            linear.Unavailable("teams", reason="TimeoutError: timed out"),
            linear.Unavailable("commentCreate", status=400, reason="RATELIMITED"),
        )
        line = {"src": "router", "kind": "linear-error"}
        cases = [
            (linear.Unavailable("teams", status=502), False, "", {"op": "teams", "status": 502}),
            (
                linear.Unavailable("issues", status=503),
                True,
                "router.py: Linear unavailable: issues: HTTP 503\n",
                None,
            ),
            (
                timeout,
                True,
                self.json(**line, op="teams", reason="TimeoutError: timed out")
                + "router.py: Linear unavailable: teams: TimeoutError: timed out\n",
                {"op": "teams", "reason": "TimeoutError: timed out"},
            ),
            (limit, False, "", {"op": "commentCreate", "status": 400, "reason": "RATELIMITED"}),
            (limit, False, "", None),
            (timeout, False, "", None),
        ]
        written = []
        for e, tty, err, new in cases:
            with self.subTest(e=str(e), tty=tty):
                self.assertEqual(self.error(e, tty=tty), (1, err))
                written += [{"ts": self.TS, **line, **new}] if new else []
                self.assertEqual(self.lines(), written)
        self.assertEqual(self.error(timeout, src="promote"), (1, ""))
        self.assertEqual(
            self.lines()[-1],
            {
                "ts": self.TS,
                "src": "promote",
                "kind": "linear-error",
                "op": "teams",
                "reason": "TimeoutError: timed out",
            },
        )

    def test_dry_prints_the_line_and_writes_nothing(self):
        e = linear.Unavailable("teams", status=503)
        for tty in (False, True):
            for _ in range(2):
                self.assertEqual(
                    self.error(e, tty=tty, dry=True),
                    (1, self.json("router", "linear-error", op="teams", status=503)),
                )
        self.assertFalse(os.path.exists(self.logs))


class ConfigurationFailures(Logs):
    def test_error_destination_deduplicates_without_changing_the_configured_log(self):
        destination = os.path.join(self.tmp, "rejected-config")
        error = ConfigError("invalid role", destination)
        code, said = self.stderr(lambda: linear.config_error("router", error))
        self.assertEqual((code, said), (1, ""))
        code, said = self.stderr(lambda: linear.config_error("router", error), tty=True)
        self.assertEqual((code, said), (1, "router.py: invalid role\n"))
        text = self.text(os.path.join(destination, "orchestrator.jsonl"))
        self.assertEqual(text, self.json("router", "config-error", reason="invalid role"))
        self.assertEqual((linear.LOGS_DIR, self.text()), (self.logs, ""))

    def test_dry_failure_prints_without_creating_either_log(self):
        destination = os.path.join(self.tmp, "rejected-config")
        code, said = self.stderr(
            lambda: linear.config_error(
                "router", ConfigError("invalid role", destination), dry=True
            )
        )
        self.assertEqual(
            (code, said), (1, self.json("router", "config-error", reason="invalid role"))
        )
        self.assertEqual((linear.LOGS_DIR, os.listdir(self.tmp)), (self.logs, []))

    def test_a_failure_without_a_destination_does_not_use_a_previous_config(self):
        self.stderr(lambda: linear.config_error("router", ConfigError("invalid work path")))
        self.assertEqual((linear.LOGS_DIR, os.listdir(self.tmp)), (self.logs, []))

    def test_plain_reasons_keep_the_configured_destination(self):
        code, said = self.stderr(lambda: linear.config_error("run", "invalid task"))
        self.assertEqual((code, said), (1, ""))
        self.assertEqual(self.text(), self.json("run", "config-error", reason="invalid task"))


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
                gql,
                "I",
                "body",
                linear.Transition(to=IN_REVIEW, frm=IN_PROGRESS, emails=("a@x.com", "b@x.com")),
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
        linear.comment_and_move(gql, "I", "body", linear.Transition(to=TODO, frm=IN_PROGRESS))
        self.assertEqual([n for n, _ in gql.calls], ["read", "move", "comment"])

    def test_skipped_or_failed_move_posts_no_comment(self):
        for state in (IN_REVIEW, TODO):
            with self.subTest(state=state):
                gql = Gql(state)
                self.assertEqual(
                    linear.comment_and_move(
                        gql,
                        "I",
                        "body",
                        linear.Transition(to=IN_REVIEW, frm=IN_PROGRESS, emails=("a@x.com",)),
                    ),
                    state,
                )
                self.assertEqual([n for n, _ in gql.calls], ["subscribe", "read"])
        gql = Gql(
            fail=lambda name, _v: LinearError("linear api error: boom") if name == "move" else None
        )
        with self.assertRaises(LinearError):
            linear.comment_and_move(
                gql, "I", "body", linear.Transition(to=IN_REVIEW, frm=IN_PROGRESS)
            )
        self.assertEqual([n for n, _ in gql.calls], ["read", "move"])

    def test_subscribe_failures_noted_in_the_comment(self):
        def fail(name, v):
            if name == "subscribe":
                return False if v["e"] == "a@x.com" else LinearError("linear api error: boom")

        gql = Gql(fail=fail)
        self.assertIsNone(
            linear.comment_and_move(
                gql,
                "I",
                "body",
                linear.Transition(to=IN_REVIEW, frm=IN_PROGRESS, emails=("a@x.com", "b@x.com")),
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
                gql,
                "I",
                "body",
                linear.Transition(to=IN_REVIEW, frm=IN_PROGRESS, emails=("a@x.com", "b@x.com")),
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
