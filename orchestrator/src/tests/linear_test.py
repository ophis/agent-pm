import io
import json
import os
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import ACCOUNTS, STATES, TASK_GROUP, TEAM, team_node  # noqa: E402
import config  # noqa: E402
import linear  # noqa: E402

LABEL1, LABEL2 = "00000000-0000-4000-8000-000000000021", "00000000-0000-4000-8000-000000000022"
OTHER = "00000000-0000-4000-8000-000000000023"


class RoleIds(unittest.TestCase):
    def test_role_ids(self):
        runs = {r: config.Role(a, f"linear-api-key-{r}", ()) for r, a in ACCOUNTS.items()}
        gql = lambda q, **v: {"users": {"nodes": [{"id": "u-" + v["e"]}]}}  # noqa: E731
        self.assertEqual(linear.role_ids(gql, runs), {f"u-{a}": r for r, a in ACCOUNTS.items()})
        with self.assertRaises(SystemExit) as cm:
            linear.role_ids(lambda q, **v: {"users": {"nodes": []}}, {"engineer": runs["engineer"]})
        self.assertEqual(cm.exception.code, "orchestrator/config.toml [roles.engineer]: account 'engineer@agents.test' not found in Linear")


class TeamCheck(unittest.TestCase):
    def cfg(self):
        return {"team": TEAM, "states": dict(STATES)}

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
        cases = {f"team {TEAM} not found in Linear": [],
                 f"[states] not workflow states of team 'Team': handoff {STATES['handoff']}, done {STATES['done']}": [team_node(other)]}
        for message, nodes in cases.items():
            with self.subTest(message):
                with self.assertRaises(SystemExit) as cm:
                    linear.team(self.gql(nodes), self.cfg())
                self.assertEqual(str(cm.exception.code), "orchestrator/config.toml: " + message)


class TaskGroupCheck(unittest.TestCase):
    LABELS = {"light-research": LABEL1, "deep-research": LABEL2}

    def cfg(self, labels=LABELS):
        return {"task_label_group": TASK_GROUP, "task_labels": labels}

    def gql(self, node=None, error=None):
        calls = []

        def gql(query, **v):
            calls.append((query, v))
            if error:
                raise SystemExit(error)
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
        self.assertIn("issueLabel(id: $i) { isGroup children(first: 250) { nodes { id } } }", linear.Q_TASK_GROUP)

    def test_fails(self):
        error = "linear api error: [{'message': 'Entity not found: IssueLabel'}]"
        not_labels = f"[task_labels] not labels of task_label_group {TASK_GROUP}: "
        cases = [
            (self.gql(self.group(LABEL1)), self.LABELS, not_labels + f"deep-research {LABEL2}"),
            (self.gql(self.group(OTHER)), {"deep-research": LABEL2, "light-research": LABEL1, "ok": OTHER},
             not_labels + f"deep-research {LABEL2}, light-research {LABEL1}"),
            (self.gql(error=error), self.LABELS, f"task_label_group {TASK_GROUP} not found in Linear: {error}"),
            (self.gql({"isGroup": False, "children": {"nodes": [{"id": LABEL1}, {"id": LABEL2}]}}), self.LABELS,
             f"task_label_group {TASK_GROUP} is not a label group"),
        ]
        for gql, labels, message in cases:
            with self.subTest(message):
                with self.assertRaises(SystemExit) as cm:
                    linear.task_group(gql, self.cfg(labels))
                self.assertEqual(str(cm.exception.code), "orchestrator/config.toml: " + message)
                self.assertEqual(len(gql.calls), 1)


class LinearGql(unittest.TestCase):
    def test_harness_key_by_service_only(self):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            return SimpleNamespace(stdout="secret\n")
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(b'{"data": {"viewer": {"id": "v"}}}')
        with mock.patch.object(linear, "harness_service", return_value="svc-h"), \
                mock.patch.object(linear.subprocess, "run", run), \
                mock.patch.object(linear.urllib.request, "urlopen", return_value=resp) as urlopen:
            self.assertEqual(linear.linear_gql("query { viewer { id } }"), {"viewer": {"id": "v"}})
        self.assertEqual(calls, [["security", "find-generic-password", "-s", "svc-h", "-w"]])
        self.assertEqual(urlopen.call_args[0][0].get_header("Authorization"), "secret")

    def test_timeout_bounds_keychain_and_request(self):
        for kw, want in (({}, 30), ({"timeout": 5}, 5)):
            with self.subTest(timeout=want):
                run = mock.Mock(return_value=SimpleNamespace(stdout="secret\n"))
                resp = mock.MagicMock()
                resp.__enter__.return_value = io.BytesIO(b'{"data": {}}')
                with mock.patch.object(linear, "harness_service", return_value="svc-h"), \
                        mock.patch.object(linear.subprocess, "run", run), \
                        mock.patch.object(linear.urllib.request, "urlopen", return_value=resp) as urlopen:
                    linear.linear_gql("query($i: String!) { issue(id: $i) { id } }", i="TASK-1", **kw)
                self.assertEqual(run.call_args.kwargs["timeout"], want)
                self.assertEqual(urlopen.call_args.kwargs["timeout"], want)
                self.assertEqual(json.loads(urlopen.call_args[0][0].data)["variables"], {"i": "TASK-1"})

    def test_harness_service_reads_the_config(self):
        linear.harness_service.cache_clear()
        self.addCleanup(linear.harness_service.cache_clear)
        self.assertEqual(linear.harness_service(), "linear-api-key")

    def test_service_names_the_keychain_item(self):
        run = mock.Mock(return_value=SimpleNamespace(stdout="secret\n"))
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(b'{"data": {}}')
        with mock.patch.object(linear, "harness_service", side_effect=AssertionError("harness key read")), \
                mock.patch.object(linear.subprocess, "run", run), \
                mock.patch.object(linear.urllib.request, "urlopen", return_value=resp) as urlopen:
            linear.linear_gql("query { viewer { id } }", service="linear-api-key-pm")
        self.assertEqual(run.call_args[0][0], ["security", "find-generic-password", "-s", "linear-api-key-pm", "-w"])
        self.assertEqual(json.loads(urlopen.call_args[0][0].data)["variables"], {})


class Lines(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "sub", "p.log")

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_stamp_is_local_time_in_the_stamp_format(self):
        self.assertEqual(linear.STAMP, "%Y-%m-%d %H:%M:%S")
        self.assertRegex(linear.stamp(), r"\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\Z")

    def test_append_creates_the_directory_and_returns_the_stamped_line(self):
        with mock.patch.object(linear, "stamp", return_value="2026-10-04 01:02:03"):
            self.assertEqual(linear.append(self.path, "end TASK-7"), "2026-10-04 01:02:03 end TASK-7")
            linear.append(self.path, "next")
        self.assertEqual(self.read(), "2026-10-04 01:02:03 end TASK-7\n2026-10-04 01:02:03 next\n")

    def test_append_refuses_a_symlink(self):
        os.makedirs(os.path.dirname(self.path))
        os.symlink(os.path.join(self.dir, "elsewhere"), self.path)
        with self.assertRaises(OSError):
            linear.append(self.path, "x")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "elsewhere")))

    def test_append_refuses_a_fifo(self):
        os.makedirs(os.path.dirname(self.path))
        os.mkfifo(self.path)
        with self.assertRaises(OSError):
            linear.append(self.path, "x")

    def test_append_replaces_a_lone_surrogate(self):
        out = linear.append(self.path, "a\udc80b")
        self.assertTrue(out.endswith(" a?b"), out)
        self.assertTrue(self.read().endswith(" a?b\n"))

    def test_issue_id_is_unanchored(self):
        self.assertEqual(re.findall(linear.ISSUE_ID, "see ENG-12, PM-3."), ["ENG-12", "PM-3"])
        for bad in ("task-7", "7-ENG", "ENG-", "ENG-7 "):
            self.assertIsNone(re.fullmatch(linear.ISSUE_ID, bad), bad)

    def test_one_line(self):
        self.assertEqual(linear.one_line("a\n\n  b\tc "), "a b c")
        self.assertEqual(linear.one_line(RuntimeError("two\nlines")), "RuntimeError: two lines")
        self.assertEqual(linear.one_line(SystemExit("api error")), "SystemExit: api error")


IN_PROGRESS, IN_REVIEW, TODO = STATES["in_progress"], STATES["in_review"], STATES["todo"]
NAMES = {linear.M_SUBSCRIBE: "subscribe", linear.Q_ISSUE_STATE: "read", linear.M_STATE: "move",
         linear.M_COMMENT: "comment"}
FIELDS = {"subscribe": "issueSubscribe", "move": "issueUpdate", "comment": "commentCreate"}


class Gql:
    """Fake Linear recording (name, variables); the issue is in `state`. fail(name, v) → an exception to raise,
    False for `success: false`, else None."""
    def __init__(self, state=IN_PROGRESS, fail=lambda name, v: None):
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


class Writes(unittest.TestCase):
    def test_queries_verbatim(self):
        self.assertEqual(linear.M_SUBSCRIBE, "mutation($i: String!, $e: String!) { issueSubscribe(id: $i, userEmail: $e) { success } }")
        self.assertEqual(linear.M_COMMENT, "mutation($i: String!, $b: String!) { commentCreate(input: { issueId: $i, body: $b }) { success } }")
        self.assertEqual(linear.M_STATE, "mutation($i: String!, $s: String!) { issueUpdate(id: $i, input: { stateId: $s }) { success } }")
        self.assertEqual(linear.Q_ISSUE_STATE, "query($i: String!) { issue(id: $i) { state { id } } }")
        self.assertEqual(linear.HISTORY, "history(first: 250, orderBy: createdAt) { nodes { createdAt actorId fromStateId toStateId } }")

    def test_call_returns_the_field_and_raises_on_success_false(self):
        gql = Gql()
        self.assertEqual(linear.call(gql, linear.M_COMMENT, "commentCreate", i="I", b="x"), {"success": True})
        self.assertEqual(gql.calls, [("comment", {"i": "I", "b": "x"})])
        with self.assertRaises(RuntimeError) as cm:
            linear.call(Gql(fail=lambda name, v: False), linear.M_COMMENT, "commentCreate", i="I", b="x")
        self.assertEqual(str(cm.exception), "commentCreate: success: false")

    def test_comment(self):
        gql = Gql()
        linear.comment(gql, "I", "hello")
        self.assertEqual(gql.calls, [("comment", {"i": "I", "b": "hello"})])
        with self.assertRaises(RuntimeError):
            linear.comment(Gql(fail=lambda name, v: False), "I", "hello")

    def test_move(self):
        for state, want, calls in ((IN_PROGRESS, None, [("read", {"i": "I"}), ("move", {"i": "I", "s": IN_REVIEW})]),
                                   (IN_REVIEW, IN_REVIEW, [("read", {"i": "I"})]),
                                   (TODO, TODO, [("read", {"i": "I"})])):
            with self.subTest(state=state):
                gql = Gql(state)
                self.assertEqual(linear.move(gql, "I", IN_REVIEW, IN_PROGRESS), want)
                self.assertEqual(gql.calls, calls)

    def test_move_raises_on_success_false(self):
        with self.assertRaises(RuntimeError):
            linear.move(Gql(fail=lambda name, v: False if name == "move" else None), "I", IN_REVIEW, IN_PROGRESS)

    def test_comment_and_move_order(self):
        gql = Gql()
        self.assertIsNone(linear.comment_and_move(gql, "I", "body", IN_REVIEW, IN_PROGRESS, ("a@x.com", "b@x.com")))
        self.assertEqual(gql.calls, [("subscribe", {"i": "I", "e": "a@x.com"}), ("subscribe", {"i": "I", "e": "b@x.com"}),
                                     ("read", {"i": "I"}), ("move", {"i": "I", "s": IN_REVIEW}),
                                     ("comment", {"i": "I", "b": "body"})])
        gql = Gql()
        linear.comment_and_move(gql, "I", "body", TODO, IN_PROGRESS)
        self.assertEqual([n for n, _ in gql.calls], ["read", "move", "comment"])

    def test_skipped_or_failed_move_posts_no_comment(self):
        for state in (IN_REVIEW, TODO):
            with self.subTest(state=state):
                gql = Gql(state)
                self.assertEqual(linear.comment_and_move(gql, "I", "body", IN_REVIEW, IN_PROGRESS, ("a@x.com",)), state)
                self.assertEqual([n for n, _ in gql.calls], ["subscribe", "read"])
        gql = Gql(fail=lambda name, v: SystemExit("linear api error: boom") if name == "move" else None)
        with self.assertRaises(SystemExit):
            linear.comment_and_move(gql, "I", "body", IN_REVIEW, IN_PROGRESS)
        self.assertEqual([n for n, _ in gql.calls], ["read", "move"])

    def test_subscribe_failures_noted_in_the_comment(self):
        def fail(name, v):
            if name == "subscribe":
                return False if v["e"] == "a@x.com" else SystemExit("linear api error: boom")
        gql = Gql(fail=fail)
        self.assertIsNone(linear.comment_and_move(gql, "I", "body", IN_REVIEW, IN_PROGRESS, ("a@x.com", "b@x.com")))
        self.assertEqual(gql.calls[-1], ("comment", {"i": "I", "b": "body"
                                                     "\n\nCould not subscribe a@x.com: RuntimeError: issueSubscribe: success: false"
                                                     "\n\nCould not subscribe b@x.com: SystemExit: linear api error: boom"}))
        self.assertEqual(linear.subscribe(Gql(), "I", ("a@x.com",)), "")

    def test_signal_exit_reraised(self):
        gql = Gql(fail=lambda name, v: SystemExit(130) if name == "subscribe" else None)
        with self.assertRaises(SystemExit) as cm:
            linear.comment_and_move(gql, "I", "body", IN_REVIEW, IN_PROGRESS, ("a@x.com", "b@x.com"))
        self.assertEqual(cm.exception.code, 130)
        self.assertEqual(gql.calls, [("subscribe", {"i": "I", "e": "a@x.com"})])


class LastMove(unittest.TestCase):
    NODES = [{"createdAt": "2026-09-27T10:00:00.000Z", "actorId": "human", "toStateId": TODO},
             {"createdAt": "2026-09-27T12:00:00.000Z", "actorId": "agent", "toStateId": TODO},
             {"createdAt": "2026-09-27T11:00:00.000Z", "actorId": "agent", "toStateId": IN_REVIEW},
             {"createdAt": "2026-09-27T13:00:00.000Z", "actorId": None, "toStateId": IN_PROGRESS}]

    def test_latest_move_into_the_states(self):
        at = linear.parse_time
        self.assertEqual(linear.last_move(self.NODES, {TODO}), at("2026-09-27T12:00:00.000Z"))
        self.assertEqual(linear.last_move(self.NODES, {TODO, IN_REVIEW}), at("2026-09-27T12:00:00.000Z"))
        self.assertEqual(linear.last_move(self.NODES, {IN_REVIEW}), at("2026-09-27T11:00:00.000Z"))
        self.assertIsNone(linear.last_move(self.NODES, {STATES["done"]}))
        self.assertIsNone(linear.last_move([], {TODO}))

    def test_by_actors(self):
        self.assertEqual(linear.last_move(self.NODES, {TODO}, {"human"}), linear.parse_time("2026-09-27T10:00:00.000Z"))
        self.assertIsNone(linear.last_move(self.NODES, {IN_PROGRESS}, {"human", "agent"}))
        self.assertIsNone(linear.last_move(self.NODES, {TODO}, set()))


if __name__ == "__main__":
    unittest.main()
