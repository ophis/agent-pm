import io
import json
import os
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


class Helpers(unittest.TestCase):
    def test_err_text(self):
        self.assertEqual(linear.SHORT, 60)
        for stderr, want in ((None, ""), ("", ""), ("  HTTP 404: Not Found \n", "HTTP 404: Not Found"), ("x" * 300, "x" * 200)):
            with self.subTest(stderr=stderr):
                self.assertEqual(linear.err_text(SimpleNamespace(stderr=stderr)), want)



class AtomicWrite(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "input.md")

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_writes_and_replaces(self):
        linear.atomic_write(self.path, "one\n")
        linear.atomic_write(self.path, "two\n")
        self.assertEqual((self.read(self.path), os.listdir(self.dir)), ("two\n", ["input.md"]))

    def test_planted_symlink_replaced_not_followed(self):
        outside = os.path.join(self.dir, "outside")
        with open(outside, "w") as f:
            f.write("keep\n")
        os.symlink(outside, self.path)
        linear.atomic_write(self.path, "new\n")
        self.assertFalse(os.path.islink(self.path))
        self.assertEqual((self.read(self.path), self.read(outside)), ("new\n", "keep\n"))

    def test_no_temp_left_on_error(self):
        linear.atomic_write(self.path, "old\n")
        with mock.patch.object(linear.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                linear.atomic_write(self.path, "new\n")
        self.assertEqual((self.read(self.path), os.listdir(self.dir)), ("old\n", ["input.md"]))


if __name__ == "__main__":
    unittest.main()
