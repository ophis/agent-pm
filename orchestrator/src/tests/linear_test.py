import io
import json
import os
import plistlib
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402,F401
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


SEAM = "AGENT_PM_LINEAR"
SECRET = "lin_api_SECRET"
VIEWER = "query { viewer { id } }"


def respond(body=b'{"data": {}}'):
    """urlopen's return value: a response reading body."""
    resp = mock.MagicMock()
    resp.__enter__.return_value = io.BytesIO(body)
    return resp


def refused(case, call):
    """The str code of the SystemExit call() raises, no request sent."""
    with mock.patch.object(linear.urllib.request, "urlopen", side_effect=AssertionError("request sent")), \
            mock.patch.object(linear.urllib.request, "build_opener", side_effect=AssertionError("request sent")), \
            case.assertRaises(SystemExit) as cm:
        call()
    case.assertIsInstance(cm.exception.code, str)
    return cm.exception.code


def no_seam(case):
    """SEAM unset for case's test."""
    env = mock.patch.dict(os.environ)
    env.start()
    case.addCleanup(env.stop)
    os.environ.pop(SEAM, None)


class LinearGql(unittest.TestCase):
    """Without the seam: the Keychain's key, api.linear.app."""
    def setUp(self):
        no_seam(self)

    def test_harness_key_by_service_only(self):
        for env in ({}, {SEAM: ""}):
            with self.subTest(env=env):
                calls = []
                def run(cmd, **kw):
                    calls.append(cmd)
                    return SimpleNamespace(stdout="secret\n")
                with mock.patch.dict(os.environ, env), mock.patch.object(linear, "harness_service", return_value="svc-h"), \
                        mock.patch.object(linear.subprocess, "run", run), \
                        mock.patch.object(linear.urllib.request, "urlopen",
                                          return_value=respond(b'{"data": {"viewer": {"id": "v"}}}')) as urlopen:
                    self.assertEqual(linear.linear_gql(VIEWER), {"viewer": {"id": "v"}})
                self.assertEqual(calls, [["/usr/bin/security", "find-generic-password", "-s", "svc-h", "-w"]])
                req = urlopen.call_args[0][0]
                self.assertEqual((req.full_url, req.get_header("Authorization")), ("https://api.linear.app/graphql", "secret"))

    def test_timeout_bounds_keychain_and_request(self):
        for kw, want in (({}, 30), ({"timeout": 5}, 5)):
            with self.subTest(timeout=want):
                run = mock.Mock(return_value=SimpleNamespace(stdout="secret\n"))
                with mock.patch.object(linear, "harness_service", return_value="svc-h"), \
                        mock.patch.object(linear.subprocess, "run", run), \
                        mock.patch.object(linear.urllib.request, "urlopen", return_value=respond()) as urlopen:
                    linear.linear_gql("query($i: String!) { issue(id: $i) { id } }", i="TASK-1", **kw)
                self.assertEqual(run.call_args.kwargs["timeout"], want)
                self.assertEqual(urlopen.call_args.kwargs["timeout"], want)
                self.assertEqual(json.loads(urlopen.call_args[0][0].data)["variables"], {"i": "TASK-1"})

    def test_harness_service_reads_the_config(self):
        linear.harness_service.cache_clear()
        self.addCleanup(linear.harness_service.cache_clear)
        with mock.patch.object(config, "load_config", return_value={"harness_key": "linear-harness"}) as load:
            self.assertEqual((linear.harness_service(), linear.harness_service()), ("linear-harness", "linear-harness"))
        load.assert_called_once_with()

    def test_urlopen_sends_it_with_the_proxies_as_configured(self):
        with mock.patch.object(linear.subprocess, "run", return_value=SimpleNamespace(stdout="secret\n")), \
                mock.patch.object(linear, "harness_service", return_value="svc-h"), \
                mock.patch.object(linear.urllib.request, "build_opener", side_effect=AssertionError("opener built")), \
                mock.patch.object(linear.urllib.request, "urlopen", return_value=respond()) as urlopen:
            linear.linear_gql(VIEWER)
        urlopen.assert_called_once()

    def test_service_names_the_keychain_item(self):
        run = mock.Mock(return_value=SimpleNamespace(stdout="secret\n"))
        with mock.patch.object(linear, "harness_service", side_effect=AssertionError("harness key read")), \
                mock.patch.object(linear.subprocess, "run", run), \
                mock.patch.object(linear.urllib.request, "urlopen", return_value=respond()) as urlopen:
            linear.linear_gql(VIEWER, service="linear-api-key-pm")
        self.assertEqual(run.call_args[0][0], ["/usr/bin/security", "find-generic-password", "-s", "linear-api-key-pm", "-w"])
        self.assertEqual(json.loads(urlopen.call_args[0][0].data)["variables"], {})

    def test_a_bad_keychain_key_raises_naming_only_the_service(self):
        for out, key in (("\n", ""), ("lin api\n", "lin api"), ("lin\x1bapi\n", "lin\x1bapi")):
            with self.subTest(key=key), \
                    mock.patch.object(linear.subprocess, "run", return_value=SimpleNamespace(stdout=out)):
                code = refused(self, lambda: linear.linear_gql(VIEWER, service="svc"))
                self.assertIn("svc", code)
                if key:
                    self.assertNotIn(key, code)


class HasKey(unittest.TestCase):
    def test_without_the_seam_the_secret_is_never_read(self):
        no_seam(self)
        for code, want in ((0, True), (44, False)):
            with mock.patch.object(linear.subprocess, "run", return_value=SimpleNamespace(returncode=code)) as m:
                self.assertIs(linear.has_key("svc"), want)
            self.assertEqual(m.call_args.args[0], ["/usr/bin/security", "find-generic-password", "-s", "svc"])


class Seam(unittest.TestCase):
    """With the seam: linear's module docstring."""
    URL = "http://127.0.0.1:8123/graphql"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        for p in (mock.patch.dict(os.environ), mock.patch.object(linear, "harness_service", return_value="svc-h"),
                  mock.patch.object(linear.subprocess, "run", side_effect=AssertionError("Keychain read"))):
            p.start()
            self.addCleanup(p.stop)

    def seam(self, data):
        """Points SEAM at a new file holding data (JSON; a str as is); None: at a missing file."""
        self.path = os.path.join(self.tmp, "missing.json")
        if data is not None:
            fd, self.path = tempfile.mkstemp(dir=self.tmp)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data if isinstance(data, str) else json.dumps(data))
        os.environ[SEAM] = self.path

    def request(self, **kw):
        """linear_gql's request."""
        with mock.patch.object(linear.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = respond()
            linear.linear_gql(VIEWER, **kw)
        return build.return_value.open.call_args[0][0]

    def assert_refused(self, *hidden):
        """linear_gql, key and has_key each raise SystemExit naming SEAM and the path, holding none of hidden."""
        for call in (lambda: linear.linear_gql(VIEWER), lambda: linear.key("svc-h", 30), lambda: linear.has_key("svc-h")):
            code = refused(self, call)
            self.assertIn(SEAM, code)
            self.assertIn(self.path, code)
            for h in hidden:
                self.assertNotIn(h, code)

    def test_url_and_keys_from_the_file_read_per_call(self):
        self.assertEqual(linear.SEAM, SEAM)
        self.seam({"url": self.URL, "keys": {"svc-h": "key-h", "svc-a": "key-a"}})
        for kw, key in (({}, "key-h"), ({"service": "svc-a"}, "key-a")):
            req = self.request(**kw)
            self.assertEqual((req.full_url, req.get_header("Authorization")), (self.URL, key))
        self.assertEqual(linear.key("svc-a", 30), "key-a")
        self.seam({"url": "http://127.0.0.1:9/graphql", "keys": {"svc-h": "key-2"}})
        req = self.request()
        self.assertEqual((req.full_url, req.get_header("Authorization")), ("http://127.0.0.1:9/graphql", "key-2"))

    def test_each_call_reads_the_file_once(self):
        self.seam({"url": self.URL, "keys": {"svc-h": "key-h", "svc-a": "key-a"}})
        for kw in ({}, {"service": "svc-a"}):
            with self.subTest(kw=kw), mock.patch.object(linear, "open", create=True, wraps=open) as opened:
                self.request(**kw)
            self.assertEqual([c.args[0] for c in opened.call_args_list], [self.path])

    def test_the_request_skips_proxies(self):
        self.seam({"url": self.URL, "keys": {"svc-h": "key-h"}})
        for name in ("HTTP_PROXY", "ALL_PROXY", "all_proxy", "no_proxy", "NO_PROXY"):
            os.environ.pop(name, None)
        os.environ["http_proxy"] = "http://proxy.invalid:3128"
        sent = []

        def do_open(handler, http_class, req, **kw):
            sent.append((req.host, req.timeout))
            resp = respond(b'{"data": {"viewer": {"id": "v"}}}')
            resp.code = 200
            return resp
        with mock.patch.object(linear.urllib.request.AbstractHTTPHandler, "do_open", do_open):
            self.assertEqual(linear.linear_gql(VIEWER, timeout=5), {"viewer": {"id": "v"}})
        self.assertEqual(sent, [("127.0.0.1:8123", 5)])

    def test_has_key_is_service_in_keys(self):
        self.seam({"url": self.URL, "keys": {"svc": SECRET}})
        self.assertEqual((linear.has_key("svc"), linear.has_key("other")), (True, False))

    def test_a_bad_file_raises_without_its_text(self):
        cases = {"unreadable": None, "not JSON": SECRET + " {", "not an object": json.dumps([self.URL, {"svc-h": SECRET}]),
                 "no url": {"keys": {"svc-h": SECRET}}, "url not a str": {"url": [self.URL], "keys": {"svc-h": SECRET}},
                 "no keys": {"url": self.URL, "key": SECRET}, "keys not an object": {"url": self.URL, "keys": [SECRET]},
                 "a key not a str": {"url": self.URL, "keys": {"svc-h": [SECRET]}}}
        for case, data in cases.items():
            with self.subTest(case):
                self.seam(data)
                self.assert_refused(SECRET)

    def test_url_guard(self):
        for url in ("http://127.0.0.1:1/graphql", "http://[::1]:1/graphql"):
            with self.subTest(url=url):
                self.seam({"url": url, "keys": {"svc-h": "k"}})
                self.assertEqual(self.request().full_url, url)
        for url in ("https://127.0.0.1:1/", "http://localhost:1/", "http://127.0.0.1/", "http://127.0.0.1:1@evil/",
                    "http://127.0.0.1.evil:1/", "http://[::1]:1@x/", "http://u:p@127.0.0.1:1/", "http://127.0.0.1:1/graphql "):
            with self.subTest(url=url):
                self.seam({"url": url, "keys": {"svc-h": SECRET}})
                self.assert_refused(url, SECRET)

    def test_a_missing_service_raises_naming_it(self):
        self.seam({"url": self.URL, "keys": {"svc-a": SECRET}})
        for call in (lambda: linear.key("svc-b", 30), lambda: linear.linear_gql(VIEWER, service="svc-b")):
            code = refused(self, call)
            self.assertIn("svc-b", code)
            self.assertNotIn(SECRET, code)

    def test_a_bad_key_raises_naming_only_the_service(self):
        for key in ("", "lin api", " lin_api", "lin_api\n", "lin\tapi", "lin\x00api", "lin\u00a0api"):
            with self.subTest(key=key):
                self.seam({"url": self.URL, "keys": {"svc-h": key}})
                code = refused(self, lambda: linear.linear_gql(VIEWER))
                self.assertIn("svc-h", code)
                if key:
                    self.assertNotIn(key, code)


class Helpers(unittest.TestCase):
    def test_issue_id_is_unanchored(self):
        self.assertEqual(re.findall(linear.ISSUE_ID, "see ENG-12, PM-3."), ["ENG-12", "PM-3"])
        for bad in ("task-7", "7-ENG", "ENG-", "ENG-7 "):
            self.assertIsNone(re.fullmatch(linear.ISSUE_ID, bad), bad)

    def test_one_line(self):
        self.assertEqual(linear.one_line("a\n\n  b\tc "), "a b c")
        self.assertEqual(linear.one_line(RuntimeError("two\nlines")), "RuntimeError: two lines")
        self.assertEqual(linear.one_line(SystemExit("api error")), "SystemExit: api error")


class Stderr(io.StringIO):
    """sys.stderr: a terminal or not; fail: the OSError each write raises (a gone tmux pane's EIO)."""
    def __init__(self, tty=False, fail=None):
        super().__init__()
        self.tty, self.fail = tty, fail

    def isatty(self):
        return self.tty

    def write(self, s):
        if self.fail:
            raise self.fail
        return super().write(s)


class Log(unittest.TestCase):
    """linear.log: the one writer of <LOGS_DIR>/orchestrator.jsonl."""
    TS = "2026-10-09T00:26:33-04:00"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.logs = os.path.join(self.tmp, "logs")
        self.path = os.path.join(self.logs, "orchestrator.jsonl")
        p = mock.patch.object(config, "LOGS_DIR", self.logs)
        p.start()
        self.addCleanup(p.stop)

    def log(self, *args, tty=False, fail=None, **kw):
        """linear.log(*args, **kw) at TS; returns its stderr."""
        err = Stderr(tty, fail)
        with mock.patch.object(sys, "stderr", err), mock.patch.object(linear.drive, "stamp", return_value=self.TS):
            linear.log(*args, **kw)
        return err.getvalue()

    def text(self, path=None):
        try:
            with open(path or self.path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def lines(self):
        return [json.loads(line) for line in self.text().splitlines()]

    def test_line_shape_and_key_order(self):
        self.assertEqual(self.log("router", "claim", "TASK-1", role="pm", task=None), "")
        self.log("prune", "prune-skip", entry="TASK-1/src/o/n", reason="not a clone é")
        self.assertEqual(self.text(), f'{{"ts": "{self.TS}", "src": "router", "kind": "claim", "issue": "TASK-1", '
                                      '"role": "pm"}\n'
                                      f'{{"ts": "{self.TS}", "src": "prune", "kind": "prune-skip", '
                                      '"entry": "TASK-1/src/o/n", "reason": "not a clone é"}\n')

    def test_the_file_is_0600_in_logs_dir_resolved_per_call(self):
        self.log("run", "end", "TASK-1", sid="s", exit=0)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        other = os.path.join(self.tmp, "other")
        with mock.patch.object(config, "LOGS_DIR", other):
            self.log("run", "end", "TASK-2", sid="s", exit=0)
        self.assertEqual([d["issue"] for d in self.lines()], ["TASK-1"])
        with open(os.path.join(other, "orchestrator.jsonl")) as f:
            self.assertEqual(json.loads(f.read())["issue"], "TASK-2")

    def test_an_oserror_from_the_file_never_raises(self):
        os.makedirs(self.logs)
        os.symlink(os.path.join(self.tmp, "elsewhere"), self.path)
        self.assertEqual(self.log("run", "end", "TASK-1", tty=True), self.json("run", "end", "TASK-1"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "elsewhere")))
        file = os.path.join(self.tmp, "file")
        open(file, "w").close()
        with mock.patch.object(config, "LOGS_DIR", os.path.join(file, "logs")):
            self.log("run", "end", "TASK-1")

    def json(self, src, kind, issue=None, **fields):
        """The stderr copy of a line."""
        line = {"ts": self.TS, "src": src, "kind": kind, **({"issue": issue} if issue else {}), **fields}
        return json.dumps(line, ensure_ascii=False) + "\n"

    def test_an_oserror_from_stderr_never_raises(self):
        for dry in (False, True):
            self.log("router", "skip", tty=True, dry=dry, fail=OSError(5, "Input/output error"), reason="x")
        self.assertEqual(len(self.lines()), 1)

    def test_a_stderr_copy_only_on_a_terminal_or_dry_and_printable(self):
        self.assertEqual(self.log("run", "tui-error", "TASK-1", msg="a"), "")
        self.assertEqual(self.log("run", "tui-error", "TASK-1", tty=True, msg="a\x9b2Jb"),
                         self.json("run", "tui-error", "TASK-1", msg="a2Jb"))
        self.assertEqual(self.lines()[-1]["msg"], "a\x9b2Jb")
        self.assertEqual(self.log("router", "pick", "TASK-2", dry=True, queue=1), self.json("router", "pick", "TASK-2", queue=1))
        self.assertEqual(len(self.lines()), 2)

    def test_dry_writes_nothing(self):
        self.log("promote", "promote", "DR-1", dry=True, role="pm")
        self.assertFalse(os.path.exists(self.logs))

    def test_once_dedups_on_src_kind_issue_entry_and_reason(self):
        base = dict(src="router", kind="usage-skip", issue=None, mode="new", reason="blocked by usage", usage="five_hour=0.95")
        cases = [({}, True), ({"usage": "five_hour=0.97", "mode": "resume"}, False), ({"reason": "other"}, True),
                 ({"issue": "TASK-1"}, True), ({"src": "promote"}, True), ({"kind": "blocked"}, True),
                 ({"entry": "TASK-1/tmp"}, True), ({}, False), ({"issue": "TASK-1"}, False)]
        for change, written in cases:
            with self.subTest(change=change):
                before = len(self.lines())
                kw = {**base, **change}
                self.log(kw.pop("src"), kw.pop("kind"), kw.pop("issue"), once=True, **kw)
                self.assertEqual(len(self.lines()) - before, int(written))
        self.log("router", "usage-skip", mode="new", reason="blocked by usage", usage="x")
        self.assertEqual(len(self.lines()), 7)

    def test_once_reads_the_last_24_hours_of_the_file(self):
        now = datetime.now().astimezone()
        recent, old = (now - timedelta(hours=h) for h in (23, 25))
        os.makedirs(self.logs)
        lines = [{"ts": recent.isoformat(), "src": "router", "kind": "blocked", "issue": "TASK-1", "by": ["TASK-7"]},
                 {"ts": old.isoformat(), "src": "router", "kind": "blocked", "issue": "TASK-2", "by": ["TASK-7"]},
                 {"ts": recent.isoformat(), "src": "prune", "kind": "prune-skip", "entry": "TASK-3/tmp", "reason": "r"}]
        junk = ["router.py: crash", "[1]", '"x"', "{", json.dumps({"src": "router", "kind": "blocked", "issue": "TASK-4"}),
                json.dumps({"ts": "yesterday", "src": "router", "kind": "blocked", "issue": "TASK-5"}),
                json.dumps({"ts": "9999-12-31T23:59:59-05:00", "src": "router", "kind": "blocked", "issue": "TASK-6"}),
                json.dumps({"ts": now.replace(tzinfo=None).isoformat(), "src": "router", "kind": "blocked", "issue": "TASK-7"})]
        with open(self.path, "w") as f:
            f.write("".join(json.dumps(d) + "\n" for d in lines) + "".join(j + "\n" for j in junk))
        for issue in ("TASK-1", "TASK-2", "TASK-4", "TASK-5", "TASK-6", "TASK-7"):
            self.log("router", "blocked", issue, once=True, by=["TASK-8"])
        self.log("prune", "prune-skip", once=True, entry="TASK-3/tmp", reason="r")
        self.assertEqual([json.loads(line)["issue"] for line in self.text().splitlines()[len(lines) + len(junk):]],
                         ["TASK-2", "TASK-4", "TASK-5", "TASK-7"])

    def test_a_dry_run_ignores_once(self):
        self.log("router", "blocked", "TASK-1", once=True, by=["TASK-7"])
        for _ in range(2):
            self.assertEqual(self.log("router", "blocked", "TASK-1", once=True, dry=True, by=["TASK-7"]),
                             self.json("router", "blocked", "TASK-1", by=["TASK-7"]))
        self.assertEqual([d["issue"] for d in self.lines()], ["TASK-1"])

    def test_once_keeps_a_cache_per_log_path(self):
        other = os.path.join(self.tmp, "other")
        for logs, written in ((self.logs, 1), (other, 1), (self.logs, 0), (other, 0)):
            with mock.patch.object(config, "LOGS_DIR", logs):
                path = os.path.join(logs, "orchestrator.jsonl")
                before = len(self.text(path).splitlines())
                self.log("router", "blocked", "TASK-1", once=True, by=["TASK-7"])
                self.assertEqual(len(self.text(path).splitlines()) - before, written, logs)

    def test_the_plists_send_launchd_output_to_the_log(self):
        for job in ("router", "promote"):
            with open(os.path.join(config.ROOT, "orchestrator", f"com.ophis.agent-pm.{job}.plist"), "rb") as f:
                plist = plistlib.load(f)
            self.assertEqual({plist["StandardOutPath"], plist["StandardErrorPath"]},
                             {"/Users/francis/.agent-pm/logs/orchestrator.jsonl"}, job)


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

    def test_move(self):
        for state, want, calls in ((IN_PROGRESS, None, [("read", {"i": "I"}), ("move", {"i": "I", "s": IN_REVIEW})]),
                                   (IN_REVIEW, IN_REVIEW, [("read", {"i": "I"})]),
                                   (TODO, TODO, [("read", {"i": "I"})])):
            with self.subTest(state=state):
                gql = Gql(state)
                self.assertEqual(linear.move(gql, "I", IN_REVIEW, IN_PROGRESS), want)
                self.assertEqual(gql.calls, calls)

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
