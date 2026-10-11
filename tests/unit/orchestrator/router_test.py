import argparse
import contextlib
import fcntl
import functools
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402,F401
from board_ids import ACCOUNTS, HEADER, STATES as IDS_BY_KEY, TASK_GROUP, TEAM, role as role_table, team_node  # noqa: E402
from run_fixtures import (CONFIG as RUN_CONFIG, DESIGN_LISTING, ENG_RUN, ENGINEER, ID, INPUT, KEY, LISTING,  # noqa: E402
                          LS_REMOTE, PM, PROJECT, RESEARCHER, SID, USER_NOTE, UUID, Base as RunBase, Gql,
                          forwarded, logged, node, res, show)
from attended_test import Tmux  # noqa: E402
from linear_test import Stderr  # noqa: E402
from outage import FAILURES, FIELDS, failing  # noqa: E402
import config  # noqa: E402
import attended  # noqa: E402
import inputs  # noqa: E402
import issues  # noqa: E402
import linear  # noqa: E402
import router  # noqa: E402
import target  # noqa: E402
import compose  # noqa: E402
import drive  # noqa: E402
import manager  # noqa: E402
import repo  # noqa: E402

NOW = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
AGENT, USER = "agent", "user"   # history actor ids: an agent account, a human_members user
STATES = {"Todo": IDS_BY_KEY["todo"], "In Progress": IDS_BY_KEY["in_progress"], "In Review": IDS_BY_KEY["in_review"]}
NAMES = {i: n for n, i in STATES.items()}
DR, PD = "Deep Research", "Product Design"
IDS = {DR: "p-dr", PD: "p-pd"}
ROLE = ACCOUNTS
CONFIG = HEADER + role_table("researcher", 'next = "pm"') + role_table("pm", 'next = "engineer"') + role_table("engineer")
CONFIG_ENG2 = HEADER + role_table("researcher", 'next = "pm"') + role_table("pm", 'next = "engineer"') + role_table("engineer", "max_runs = 2")


def setUpModule():
    tmp = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(tmp.cleanup)
    for p in (mock.patch.object(router, "RUNS_DIR", tmp.name), mock.patch.object(config, "RUNS_DIR", tmp.name)):
        p.start()
        unittest.addModuleCleanup(p.stop)
LIST = ["tmux", "list-sessions", "-F", "#{session_name}"]
LIGHT, DEEP, STRAY, ORPHAN_LABEL = (f"00000000-0000-4000-8000-0000000000{n}" for n in (21, 22, 23, 24))   # label ids; STRAY maps to no task
# ORPHAN_LABEL picks pm's task, none of researcher's.
TASK_LABELS = f'[task_labels]\nlight-research = "{LIGHT}"\ndeep-research = "{DEEP}"\nproduct-design = "{ORPHAN_LABEL}"\n'
RECHECK = "query($i: String!) { issue(id: $i) { state { id } labels { nodes { id name parent { id } } } } }"


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def who(role):
    """assignee node of a role account, or of someone else for a non-role name."""
    return None if role is None else {"id": f"u-{role}", "email": ROLE.get(role, f"{role}@x.com")}


class FakeLinear:
    def __init__(self, issues, history=None):
        self.issues = {i["identifier"]: i for i in issues}
        self.history = {} if history is None else history
        self.mutations = []
        self.queries = []
        self.todo_error = False
        self.unreadable = set()
        self.refuse = set()   # issues whose state change returns success: false
        # the task_label_group's issueLabel node, its children the labels TaskLabels maps
        self.group = {"isGroup": True, "children": {"nodes": [{"id": i} for i in (LIGHT, DEEP, ORPHAN_LABEL)]}}

    def __call__(self, query, **v):
        self.queries.append((query, v))
        if query == linear.Q_TEAM:
            return {"teams": {"nodes": [team_node()]}}
        if query == linear.Q_TASK_GROUP:
            return {"issueLabel": self.group}
        if "users(filter" in query:
            users = {"me@x.com": USER, "b@x.com": "user-b", **{a.lower(): f"u-{r}" for r, a in ROLE.items()}}
            return {"users": {"nodes": [{"id": u} for e, u in users.items() if v["e"].lower() == e]}}
        if "history" in query:
            return {"issue": {"history": {"nodes": self.history.get(v["i"], [])}}}
        if "mutation" in query:
            self.mutations.append((query, v))
            issue = self.issues[v["i"]]
            if "issueSubscribe" in query:
                issue.setdefault("subscribers", []).append(v["e"])
            elif "commentCreate" in query:
                issue.setdefault("comments", []).append(v["b"])
            elif v["i"] in self.refuse:
                return {"issueUpdate": {"success": False}}
            else:
                issue["state"] = NAMES[v["s"]]
            return {op(query): {"success": True}}
        if query == linear.Q_ISSUE_STATE:
            return {"issue": {"state": {"id": STATES[self.issues[v["i"]]["state"]]}}}
        if "issues(filter" in query:
            f = v["f"]
            if self.todo_error and "inverseRelations" in query:
                raise SystemExit("linear api error: [{'message': 'Entity not found'}]")
            drop = {"state", "labels"} if "inverseRelations" in query else {"state", "labels", "inverseRelations"}
            return {"issues": {"nodes": [{k: x for k, x in i.items() if k not in drop} for i in self.issues.values()
                                         if f["team"]["id"]["eq"] == TEAM and f["project"] == {"null": False} and i["project"]
                                         and i["assignee"] and i["assignee"]["id"] in f["assignee"]["id"]["in"]
                                         and i["state"] == NAMES[f["state"]["id"]["eq"]]]}}
        if "issue(id:" in query and "inverseRelations" in query:
            if v["i"] in self.unreadable:
                raise SystemExit("linear api error: [{'message': 'Entity not found'}]")
            return {"issue": {"inverseRelations": self.issues[v["i"]]["inverseRelations"]}}
        if query == RECHECK:
            i = self.issues[v["i"]]
            return {"issue": {"state": {"id": STATES[i["state"]]}, "labels": {"nodes": i["labels"]}}}
        raise AssertionError(query)

    def reads(self, state="Todo"):
        return [q for q, v in self.queries if "issues(filter" in q and v["f"].get("state") == {"id": {"eq": STATES[state]}}]


def op(query):
    return re.search(r"\{ (\w+)\(", query).group(1)


def writes(fake):
    """(operation, issue) of each mutation and each move's state read ("read"), in order."""
    return [("read" if q == linear.Q_ISSUE_STATE else op(q), v["i"]) for q, v in fake.queries
            if q == linear.Q_ISSUE_STATE or "mutation" in q]


def issue(ident, state, role=None, updated=None, priority=0, created="2026-09-01T00:00:00Z", project=DR, inverse=(), labels=()):
    # id == identifier so mutations and history can be keyed by either
    return {"id": ident, "identifier": ident, "url": f"https://linear.app/x/{ident}", "state": state,
            "project": project and {"id": IDS[project], "name": project},
            "assignee": who(role), "priority": priority, "createdAt": created, "updatedAt": updated or ago(minutes=5),
            "inverseRelations": {"nodes": list(inverse)}, "labels": list(labels)}


def blocker(ident, state="started", kind="blocks"):
    """An inverse relation node: ident (None = unreadable) relates to the issue by kind; state is ident's state type."""
    return {"type": kind, "issue": ident and {"identifier": ident, "state": {"type": state}}}


def label(name, id, group=TASK_GROUP):
    """A labels node of the re-check: id and name in the label group (None = not in a group)."""
    return {"id": id, "name": name, "parent": group and {"id": group}}


class Moved(FakeLinear):
    """A human moves every issue to state `to` right after each list read."""
    def __init__(self, issues, to):
        super().__init__(issues)
        self.to = to

    def __call__(self, query, **v):
        out = super().__call__(query, **v)
        if "issues(filter" in query:
            for i in self.issues.values():
                i["state"] = self.to
        return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        logs = tempfile.TemporaryDirectory()  # outside self.tmp, whose listing tests check
        self.addCleanup(logs.cleanup)
        self.logs_root, self.logs, self.mark = logs.name, None, 0
        self.tdir = os.path.join(self.tmp.name, "transcripts")
        os.makedirs(self.tdir)
        self.log = os.path.join(self.tmp.name, "runs.jsonl")
        self.lines = []
        self.hist = {}
        self.names = {}
        self.config = self.write_config(CONFIG)
        self.root = config.ROOT

    def sid(self, name):
        """A UUID for a short session name (transcript() accepts UUIDs only); outputs map it back to the name."""
        s = str(uuid.uuid5(uuid.NAMESPACE_OID, name))
        self.names[s] = name
        return s

    def unmap(self, text):
        for s, name in self.names.items():
            text = text.replace(s, name)
        return text

    def write_config(self, text):
        path = os.path.join(self.tmp.name, "cfg", "config.toml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def fake(self, *issues):
        return FakeLinear(issues, self.hist)

    def moved(self, ident, minutes_ago, state=STATES["In Progress"], actor=AGENT):
        self.hist.setdefault(ident, []).insert(0, {"createdAt": ago(minutes=minutes_ago), "actorId": actor, "toStateId": state})

    def resumable(self, ident, sid, minutes_ago, role="researcher"):
        """A start line for sid, an old <sid>.jsonl, and a move to In Progress just before the start."""
        self.moved(ident, minutes_ago + 1)
        self.add("start", ident, sid, minutes_ago, role)
        self.touch(ident, sid, 40)

    def row(self, kind, ident, sid, role="researcher", **ago):
        """A runs.jsonl line `ago` before NOW, its ts at a fixed UTC-4 offset."""
        ts = (NOW - timedelta(**ago)).astimezone(timezone(timedelta(hours=-4))).isoformat(timespec="seconds")
        return json.dumps({"ts": ts, "kind": kind, "issue": ident, "sid": self.sid(sid), "role": role})

    def add(self, kind, ident, sid, minutes_ago, role="researcher"):
        """A runs.jsonl line."""
        self.lines.append(self.row(kind, ident, sid, role, minutes=minutes_ago))

    def touch(self, ident, sid, minutes_ago, sub=None):
        """<sid>.jsonl, or sub under <sid>/, in the issue's transcript folder."""
        p = config.transcript(ident, self.sid(sid), self.tdir)
        if sub:
            p = os.path.join(p[:-len(".jsonl")], sub)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
        t = (NOW - timedelta(minutes=minutes_ago)).timestamp()
        os.utime(p, (t, t))

    def log_dir(self, again):
        """config.LOGS_DIR patched for one board or tick: a fresh dir, or (again) the last one, as the next tick finds it."""
        if not again or self.logs is None:
            self.logs = tempfile.mkdtemp(dir=self.logs_root)
        self.mark = len(logged(self.logs))
        return mock.patch.object(config, "LOGS_DIR", self.logs)

    def board(self, fake, use, dry=False):
        """use(Board) over runs.jsonl lines; its stderr lands in self.err."""
        with open(self.log, "w") as f:
            f.write("\n".join(self.lines) + "\n")
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(False):
            out = use(router.Board(fake, router.parse_log(self.log), self.tdir, NOW, dry, config.load_config(self.config),
                                   root=self.root))
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        return out

    def plan(self, fake, dry=False):
        """Recover, then "resume <ID> <SID> <url> <project>", "new" or ""."""
        run = self.board(fake, lambda b: b.next_run(b.recover()), dry)
        if run and run[0] == "resume":
            _, issue, sid = run
            return self.unmap(f"resume {issue['identifier']} {sid} {issue['url']} {issue['project']['name']}")
        return "new" if run else ""

    def claim(self, fake, dry=False, full=(), recover=False):
        """Take with the full roles, after Recover if asked: "<ID> <url> <project>" or ""."""
        def use(b):
            recover and b.recover()
            return b.take(full=full)
        taken = self.board(fake, use, dry)
        return taken and f"{taken[0]['identifier']} {taken[0]['url']} {taken[0]['project']['name']}" or ""

    def said(self):
        """The router's events of the last board or tick, logged or (dry) copied to stderr, as show() gives them."""
        events = logged(self.logs)[self.mark:] + [json.loads(x) for x in self.raw.splitlines() if x.startswith("{")]
        self.assertEqual({e["src"] for e in events} - {"router"}, set())
        return [self.unmap(show(e)) for e in events]

    def tick(self, fake, *argv, hour=2, shell=None, again=False, now=NOW):
        """router.main with argv (a tick, or --issue), the outer faked by self.sh.start; again: on the last log."""
        self.sh = shell or FakeShell()
        with open(self.log, "w") as f:
            f.write("\n".join(self.lines) + ("\n" if self.lines else ""))
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}), \
                self.log_dir(again):
            rc = router.main(list(argv), gql=fake, now=now, tdir=self.tdir, config=self.config, runs=self.log,
                             sh=self.sh, hour=hour, root=self.root, start=self.sh.start)
            self.path = os.environ["PATH"]
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        with open(self.log) as f:
            self.state = self.unmap(f.read())
        return rc

    def last_run(self):
        """(kind, issue, sid, role) of the last runs.jsonl line `tick` left, after checking its keys."""
        entry = json.loads(self.state.splitlines()[-1])
        self.assertEqual(list(entry), ["ts", "kind", "issue", "sid", "role"])
        return entry["kind"], entry["issue"], entry["sid"], entry["role"]

    def launched(self):
        (launch,) = self.sh.launches()
        return launch.issue

    def launched_runs(self):
        """(ID, mode) of each outer call, in order."""
        return [(a.issue, a.mode) for a in self.sh.launches()]

    def probes(self):
        return [c[0] for c in self.sh.calls].count("claude")


class ParseAndLiveness(Base):
    def write_lines(self, lines):
        with open(self.log, "w") as f:
            f.write("".join(line + "\n" for line in lines))

    def test_parse_log_is_utc_entries_in_file_order(self):
        self.assertEqual(router.parse_log(os.path.join(self.tmp.name, "nope")), [])   # a missing log is empty
        self.add("start", "TASK-1", "a", 60, "researcher")
        self.add("start", "TASK-2", "b", 50, "pm")
        self.add("resume", "TASK-1", "a", 40, "pm-lead")
        self.write_lines(self.lines)
        entries = router.parse_log(self.log)
        self.assertEqual(entries, [(NOW - timedelta(minutes=60), "start", "TASK-1", self.sid("a"), "researcher"),
                                   (NOW - timedelta(minutes=50), "start", "TASK-2", self.sid("b"), "pm"),
                                   (NOW - timedelta(minutes=40), "resume", "TASK-1", self.sid("a"), "pm-lead")])
        self.assertEqual({e[0].utcoffset() for e in entries}, {timedelta(0)})

    def test_parse_log_skips_what_is_not_a_complete_start_or_resume_line(self):
        good = json.loads(self.row("start", "TASK-1", "a", minutes=60))
        bad = ["not json", "", "[]", '"start"', "7", "null", "{",
               "2026-09-26 23:00:00 start TASK-3 session=c role=x",
               "2026-09-26 23:00:00 start TASK-4 session=d task=deep-research",
               json.dumps({k: v for k, v in good.items() if k != "role"}),
               json.dumps({k: v for k, v in good.items() if k != "ts"}),
               json.dumps({**good, "role": None}), json.dumps({**good, "role": 3}),
               json.dumps({**good, "issue": ["TASK-2"]}), json.dumps({**good, "sid": None}),
               json.dumps({**good, "kind": "end"}), json.dumps({**good, "kind": ["start"]}),
               json.dumps({**good, "ts": "yesterday"}), json.dumps({**good, "ts": 5}), json.dumps({**good, "ts": None}),
               json.dumps({**good, "ts": "9999-12-31T23:59:59-05:00"})]
        self.add("resume", "TASK-9", "z", 30, "pm")
        self.write_lines([*bad, json.dumps(good), *bad, self.lines[-1]])
        self.assertEqual([e[1:] for e in router.parse_log(self.log)],
                         [("start", "TASK-1", self.sid("a"), "researcher"), ("resume", "TASK-9", self.sid("z"), "pm")])

    def test_liveness(self):
        s, t = self.sid("s"), self.sid("t")
        self.assertFalse(router.is_live(self.tdir, "TASK-1", s, NOW))
        self.touch("TASK-1", "s", 40)
        self.assertFalse(router.is_live(self.tdir, "TASK-1", s, NOW))
        self.touch("TASK-1", "s", 10, "subagents/workflows/r/journal.jsonl")
        self.assertTrue(router.is_live(self.tdir, "TASK-1", s, NOW))
        self.touch("TASK-1", "t", 29)
        self.assertTrue(router.is_live(self.tdir, "TASK-1", t, NOW))
        self.assertFalse(router.is_live(self.tdir, "TASK-2", t, NOW))
        self.assertFalse(router.is_live(self.tdir, "TASK-1", "not-a-uuid", NOW))
        self.touch("TASK-2", "s", 5, "subagents/workflows/r/journal.jsonl")
        self.assertTrue(router.is_live(self.tdir, "TASK-2", s, NOW))


def event(status="allowed", five=0.1, **week):
    windows = {"five_hour": {"utilization": five, "resetsAt": 1}}
    windows.update({k: {"utilization": v, "resetsAt": 2} for k, v in week.items()})
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "utilization": five, "rateLimitType": "five_hour", "resetsAt": 1, "unifiedWindows": windows}})


class Gate(unittest.TestCase):
    def test_ok_from_the_last_event(self):
        self.assertEqual(router.gate(['{"type":"system"}', "garbage"]), (False, "no rate_limit_event"))
        self.assertEqual([router.gate([event(five=0.85)], *m)[0] for m in ((), (0.8,))], [True, False])   # max_5h
        rows = (([event(five=0.89)], True),
                ([event(five=0.9)], False),
                ([event(status="rejected", five=0.1)], False),
                ([event(five=0.1, seven_day=1.0)], False),
                ([event(five=0.89, seven_day=0.99, seven_day_opus=0.5)], True),
                ([event(five=0.1, seven_day=0.2, seven_day_opus=1.0)], False),
                ([event(status="allowed_warning", five=0.5)], True),
                ([event(five=0.1), event(five=0.9)], False),
                ([event(five=0.9), "{}", event(five=0.1)], True))
        for row, (lines, ok) in enumerate(rows):
            with self.subTest(row=row):
                self.assertEqual(router.gate(lines)[0], ok)


class Brake(unittest.TestCase):
    def brake(self, *lines):
        sh = mock.Mock(return_value=subprocess.CompletedProcess(router.PROBE, 0, stdout="".join(f"{x}\n" for x in lines)))
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            rc = router.main(["--brake"], gql=None, sh=sh)
            self.path = os.environ["PATH"]
        self.sh = sh
        return rc, out.getvalue()

    def test_probe(self):
        self.assertEqual(router.PROBE, ["claude", "-p", "Reply with OK.", "--model", "haiku", "--output-format", "stream-json",
                                        "--verbose", "--setting-sources", "user", "--strict-mcp-config"])
        self.assertEqual(self.brake(event(five=0.79, seven_day=0.3)), (0, "status=allowed five_hour=0.79 seven_day=0.3\n"))
        self.sh.assert_called_once_with(router.PROBE, cwd=router.RUNS_DIR, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(self.path, router.PATH)

    def test_blocks_at_brake_5h(self):
        self.assertEqual(router.BRAKE_5H, 0.8)
        rows = ((event(five=0.79), 0), (event(five=0.8), 1), (event(status="rejected", five=0.1), 1), ('{"type":"system"}', 1))
        for line, rc in rows:
            with self.subTest(line):
                self.assertEqual(self.brake(line)[0], rc)
        self.assertEqual(self.brake("garbage"), (1, "no rate_limit_event\n"))


class Plan(Base):
    def test_new_or_nothing_without_a_change(self):
        rows = (("nothing", [issue("TASK-1", "In Review", "researcher")], ""),
                ("new", [issue("TASK-1", "Todo", "researcher")], "new"),
                ("other assignees not recovered", [issue("TASK-1", "In Progress", "someone", updated=ago(hours=3)),
                                                   issue("TASK-2", "In Progress", None, updated=ago(hours=3))], ""))
        for name, issues_, out in rows:
            with self.subTest(name):
                fake = FakeLinear(issues_)
                self.assertEqual(self.plan(fake), out)
                self.assertEqual(fake.mutations, [])

    def test_attempt_cap_beats_resume(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=5)))
        self.resumable("TASK-1", "sid1", 300)
        for m in (200, 150, 100):
            self.add("resume", "TASK-1", "sid1", m)
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])

    def test_live_session_not_resumed_or_recovered(self):
        resume = "resume TASK-3 c https://linear.app/x/TASK-3 Deep Research"
        for others, out in (((), ""), ((issue("TASK-2", "Todo", "researcher"),), "new"),
                            ((issue("TASK-3", "In Progress", "researcher", priority=3),), resume)):
            with self.subTest(out=out):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=1, updated=ago(hours=5)), *others)
                self.resumable("TASK-1", "a", 300)
                self.touch("TASK-1", "a", 5, "subagents/workflows/r/journal.jsonl")
                if any(i["state"] == "In Progress" for i in others):
                    self.resumable("TASK-3", "c", 100)
                self.assertEqual(self.plan(fake), out)
                self.assertEqual(fake.mutations, [])

    def test_live_session_issue_skipped_first_and_every_candidate_returned(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=1, updated=ago(hours=3)),
                         issue("TASK-2", "In Progress", "researcher", priority=2, updated=ago(hours=3)),
                         issue("TASK-3", "In Progress", "researcher", priority=3), issue("TASK-4", "In Progress", "pm", priority=4, project=PD))
        for m in (390, 380, 370, 360):
            self.add("start", "TASK-1", f"a{m}", m)
        self.resumable("TASK-3", "c", 100)
        self.resumable("TASK-4", "d", 200, "pm")
        cands = self.board(fake, lambda b: b.recover({"TASK-1"}))
        self.assertEqual([(i["identifier"], self.unmap(sid)) for i, sid in cands], [("TASK-3", "c"), ("TASK-4", "d")])
        self.assertEqual(self.said(), [f"recover TASK-2 to=todo reason=no session updated={ago(hours=3)}"])
        self.assertEqual({i: t["state"] for i, t in fake.issues.items()},
                         {"TASK-1": "In Progress", "TASK-2": "Todo", "TASK-3": "In Progress", "TASK-4": "In Progress"})
        self.assertNotIn("comments", fake.issues["TASK-1"])
        self.assertNotIn("TASK-1", [v["i"] for q, v in fake.queries if "history" in q])

    def test_resume_order(self):
        r = "researcher"
        rows = (((("TASK-1", 3, 300, r, DR), ("TASK-2", 2, 100, r, DR)), DR),
                ((("TASK-1", 2, 100, r, DR), ("TASK-2", 2, 300, r, DR)), DR),
                ((("TASK-1", 0, 300, r, DR), ("TASK-2", 4, 100, r, DR)), DR),
                ((("TASK-1", 2, 120, r, DR), ("TASK-2", 2, 60, "pm", PD)), PD))
        for specs, project in rows:
            with self.subTest(specs=specs):
                self.lines, self.hist = [], {}
                fake = self.fake(*[issue(i, "In Progress", role, priority=p, project=proj) for i, p, _, role, proj in specs])
                for ident, _, m, role, _ in specs:
                    self.resumable(ident, ident.lower(), m, role)
                self.assertEqual(self.plan(fake), f"resume TASK-2 task-2 https://linear.app/x/TASK-2 {project}")

    def test_no_current_sid_never_outranks(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=1), issue("TASK-2", "In Progress", "researcher", priority=4))
        self.add("start", "TASK-1", "old", 400)
        self.touch("TASK-1", "old", 300)
        self.moved("TASK-1", 60, actor=USER)
        self.resumable("TASK-2", "b", 100)
        self.assertEqual(self.plan(fake), "resume TASK-2 b https://linear.app/x/TASK-2 Deep Research")

    def test_sid_without_start_sorts_by_first_line(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=2), issue("TASK-2", "In Progress", "researcher", priority=2))
        self.add("resume", "TASK-1", "a", 300)
        self.add("resume", "TASK-1", "a", 50)
        self.touch("TASK-1", "a", 40)
        self.resumable("TASK-2", "b", 200)
        self.assertEqual(self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research")

    def walk_fixture(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=2), issue("TASK-4", "In Progress", "researcher", priority=2),
                         issue("TASK-3", "In Progress", "researcher", priority=3), issue("TASK-5", "In Progress", "researcher", priority=4),
                         issue("TASK-6", "In Progress", "researcher", priority=4), issue("TASK-9", "Todo", "researcher", priority=1))
        self.moved("TASK-1", 400)
        self.add("start", "TASK-1", "a", 399)
        self.resumable("TASK-3", "c", 100)
        self.resumable("TASK-6", "f", 50)
        for ident, sid in (("TASK-4", "d"), ("TASK-5", "e")):
            for m in (390, 380, 370, 360):
                self.add("start", ident, f"{sid}{m}", m)
        return fake

    def test_walk_moves_before_and_after_candidate(self):
        resume = "resume TASK-3 c https://linear.app/x/TASK-3 Deep Research"
        for mode, out in (("plan", resume), ("dry plan", resume), ("pick", "TASK-9 https://linear.app/x/TASK-9 Deep Research")):
            with self.subTest(mode=mode):
                self.lines, self.hist = [], {}
                fake = self.walk_fixture()
                self.assertEqual(self.claim(fake, recover=True) if mode == "pick" else self.plan(fake, dry=mode == "dry plan"), out)
                self.assertIn("recover TASK-1 to=todo reason=no transcript sid=a", self.said())
                self.assertIn("recover TASK-5 to=in_review reason=reached 4 attempts", self.said())
                if mode == "dry plan":
                    self.assertEqual(fake.mutations, [])
                    continue
                states = {i: fake.issues[i]["state"] for i in ("TASK-1", "TASK-3", "TASK-4", "TASK-5", "TASK-6")}
                self.assertEqual(states, {"TASK-1": "Todo", "TASK-3": "In Progress", "TASK-4": "In Review",
                                          "TASK-5": "In Review", "TASK-6": "In Progress"})
                self.assertEqual(fake.issues["TASK-1"]["comments"][0], router.INTERRUPTED)
                self.assertNotIn("comments", fake.issues["TASK-3"])

    def test_sid_without_transcript(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
        self.add("start", "TASK-1", "a", 10)
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.mutations, [])
        self.lines = []
        self.add("start", "TASK-1", "a", 40)
        self.assertEqual(self.plan(fake), "new")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("Todo", who("researcher")))

    def test_no_sid_keeps_two_hour_rule(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "In Progress", "engineer", updated=ago(hours=3), project=PD),
                           issue("TASK-2", "In Progress", "researcher", updated=ago(hours=1))])
        self.plan(fake)
        t = fake.issues["TASK-1"]
        self.assertEqual((t["state"], t["assignee"], t["comments"]), ("Todo", who("engineer"), [router.INTERRUPTED]))
        self.assertNotIn("subscribers", t)
        self.assertFalse(any("assigneeId" in repr(v) for _, v in fake.mutations))
        self.assertEqual(fake.issues["TASK-2"]["state"], "In Progress")

    def test_stale_sid_goes_to_two_hour_rule(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=220)))
        self.add("start", "TASK-1", "old", 400)
        self.touch("TASK-1", "old", 400)
        self.moved("TASK-1", 220, actor=USER)
        self.assertEqual(self.plan(fake), "new")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_tolerance(self):
        for start, want in ((100.05, "resume"), (106, "")):
            self.lines, self.hist = [], {}
            fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
            self.moved("TASK-1", 100)
            self.add("start", "TASK-1", "a", start)
            self.touch("TASK-1", "a", 40)
            self.assertEqual(self.plan(fake).split(" ")[0], want)

    def test_no_move_in_history_is_current(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.add("start", "TASK-1", "a", 100)
        self.touch("TASK-1", "a", 40)
        self.assertEqual(self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research")

    def test_no_current_sid_never_capped(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=1)))
        for m in (600, 550, 500, 450):
            self.add("start", "TASK-1", f"s{m}", m)
        self.moved("TASK-1", 60, actor=USER)
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.mutations, [])

    def test_launch_failures_end_in_review(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        for n in range(4):
            self.moved("TASK-1", 41)
            self.add("start", "TASK-1", f"s{n}", 40)
            if n < 3:
                self.assertEqual(self.plan(fake), "new")
                self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")
                self.assertEqual(self.claim(fake), "TASK-1 https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"][-1], router.CAP_COMMENT)

    def reset_fixture(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        self.moved("TASK-1", 700)
        self.add("start", "TASK-1", "old", 699)
        for m in (650, 600, 550):
            self.add("resume", "TASK-1", "old", m)
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.resumable("TASK-1", "new", 100)
        self.add("resume", "TASK-1", "new", 60)

    def test_user_reset_resumes_and_orders_by_new_sid(self):
        rows = (("resumes", (), "resume TASK-1 new https://linear.app/x/TASK-1 Deep Research"),
                ("orders", (issue("TASK-2", "In Progress", "researcher", priority=2),),
                 "resume TASK-2 b https://linear.app/x/TASK-2 Deep Research"))
        for name, others, out in rows:
            with self.subTest(name):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=2), *others)
                self.reset_fixture()
                if others:
                    self.resumable("TASK-2", "b", 200)
                self.assertEqual(self.plan(fake), out)
                self.assertEqual(fake.mutations, [])

    def test_attempt_cap_subscribes_humans(self):
        for members in ([], ["me@x.com", "b@x.com"]):
            with self.subTest(members=members):
                self.config = self.write_config((f"human_members = {json.dumps(members)}\n" if members else "") + CONFIG)
                fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=40))])
                self.lines = []
                for i, sid in enumerate("abcd"):
                    self.add("start", "TASK-1", sid, 400 - i * 50)
                self.assertEqual(self.plan(fake), "")
                t = fake.issues["TASK-1"]
                self.assertEqual((t["state"], t["assignee"], t["comments"], t.get("subscribers", [])),
                                 ("In Review", who("researcher"), [router.CAP_COMMENT], members))
                self.assertEqual(writes(fake), [("issueSubscribe", "TASK-1")] * len(members)
                                 + [("read", "TASK-1"), ("issueUpdate", "TASK-1"), ("commentCreate", "TASK-1")])
                self.assertEqual(fake.mutations[-2][1]["s"], STATES["In Review"])

    def test_unknown_human_fails_loud(self):
        self.config = self.write_config('human_members = ["nobody@x.com"]\n' + CONFIG)
        with self.assertRaises(SystemExit) as e:
            self.plan(FakeLinear([]))
        self.assertIn("nobody@x.com", str(e.exception))

    def capped(self, hist):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))], {"TASK-1": hist})
        for i, sid in enumerate("abcd"):
            self.add("start", "TASK-1", sid, 400 - i * 50)
        self.touch("TASK-1", "d", 200)
        return fake, self.plan(fake)

    def test_attempt_cap_reset_only_by_a_human_members_move_to_todo(self):
        def move(m, actor, state="Todo"):
            return {"createdAt": ago(minutes=m), "actorId": actor, "toStateId": STATES[state]}
        rows = (("user", [move(380, USER)], "resume TASK-1 d https://linear.app/x/TASK-1 Deep Research", "In Progress"),
                ("not a human member", [move(380, AGENT), move(375, "role-account"), move(370, USER, "In Progress"),
                                        move(360, None)], "", "In Review"))
        for name, hist, out, state in rows:
            with self.subTest(name):
                self.lines = []
                fake, got = self.capped(hist)
                self.assertEqual((got, fake.issues["TASK-1"]["state"]), (out, state))
                if state == "In Progress":
                    self.assertEqual(fake.mutations, [])

    def test_skipped_move_posts_no_comment(self):
        fake = Moved([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))], "In Review")
        self.assertEqual(self.plan(fake), "")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("In Review", []))
        self.assertNotIn("comments", fake.issues["TASK-1"])
        self.assertEqual(writes(fake), [("read", "TASK-1")])
        self.assertIn(f"move-skip TASK-1 step=recover state={STATES['In Review']}", self.said())

    def test_one_history_fetch_per_issue(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        queries = []
        self.plan(lambda q, **v: queries.append(q) or fake(q, **v))
        self.assertEqual(sum("history" in q for q in queries), 1)
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")


class Prune(Base):
    def write(self, lines):
        with open(self.log, "w") as f:
            f.write("".join(line + "\n" for line in lines))

    def read(self):
        with open(self.log) as f:
            return f.read()

    def test_drops_old_and_unparsable_lines(self):
        keep = [self.row("start", "TASK-1", "b", days=6), self.row("resume", "TASK-1", "b", hours=1),
                self.row("start", "TASK-2", "c", days=7)]
        junk = [self.row("start", "TASK-1", "a", days=8), "plain text", "", "[1, 2]", '{"ts": "x"}',
                self.row("start", "TASK-1", "a", days=7, seconds=1),
                json.dumps({**json.loads(keep[0]), "ts": "yesterday"}),
                json.dumps({**json.loads(keep[0]), "kind": "end"})]
        self.write([junk[0], keep[0], *junk[1:4], keep[1], *junk[4:], keep[2]])
        with mock.patch.object(router.drive.os, "replace", wraps=os.replace) as rep:
            router.prune(self.log, NOW)
        [(src, dst), _] = rep.call_args
        self.assertEqual((os.path.dirname(src), str(dst)), (self.tmp.name, self.log))
        self.assertEqual(self.read(), "".join(line + "\n" for line in keep))
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["cfg", "runs.jsonl", "transcripts"])
        self.write([keep[1], "plain text"])  # unparsable lines alone, no old one, still a rewrite
        router.prune(self.log, NOW)
        self.assertEqual(self.read(), keep[1] + "\n")

    def test_no_temp_left_when_the_rewrite_fails(self):
        self.write([self.row("start", "TASK-1", "a", days=8), self.row("start", "TASK-1", "b", hours=1)])
        with mock.patch.object(router.drive.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                router.prune(self.log, NOW)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["cfg", "runs.jsonl", "transcripts"])
        self.assertEqual(len(self.read().splitlines()), 2)

    def test_unchanged_file_not_rewritten(self):
        lines = [self.row("start", "TASK-1", "a", days=1), self.row("resume", "TASK-1", "a", minutes=5)]
        self.write(lines)
        with mock.patch.object(router.drive.os, "replace") as rep, mock.patch.object(router.drive, "save") as save:
            router.prune(self.log, NOW)
        rep.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.read(), "".join(line + "\n" for line in lines))

    def test_missing_log_is_noop(self):
        router.prune(self.log, NOW)
        self.assertFalse(os.path.exists(self.log))


class Usage(unittest.TestCase):
    def test_unknown_flags_rejected(self):
        for argv in (["--help"], ["--prune", "x"], ["--gate", "new"], ["--gate", "resume"],
                     ["-h"], ["--now", "--issue", "TASK-1"], ["--issue", "TASK-1", "--now"],
                     ["--brake", "new"], ["--brake", "--dry-run"], ["--dry-run", "--brake"], ["--brake", "--brake"],
                     ["--now", "--issue", "TASK-1", "--tui"], ["--tui", "--brake"], ["--issue", "TASK-1", "--brake"],
                     ["--brake", "--tui"], ["--split", "right"], ["--now", "--split-from", "dev"], ["--dry-run", "--split", "below"],
                     ["--tui", "--split"], ["--tui", "--split-from", "--now"], ["--now", "--issue", "--dry-run"],
                     ["--tui", "--split", "right", "--split", "below"], ["--tui", "dev"], ["--split=right"],
                     ["--now", "--split-from=dev"], ["--tui", "--split=right", "--split=below"],
                     ["--tui", "--split-from=dev", "--split-from", "dev"], ["--now", "--issue=TASK-1"], ["--tui", "--now=x"],
                     ["--events", "x"], ["--issue", "TASK-1", "--events=x"], ["--tui", "--events"],
                     ["--tui", "--events", "a", "--events", "b"], ["--issue", "TASK-1", "--issue", "TASK-2"], ["--issue"],
                     ["--issue", "TASK-1", "--split", "below"], ["--manager", "m1"], ["--manager=m1"],
                     ["--now", "--manager", "m1"], ["--issue", "TASK-1", "--manager=m1"], ["--tui", "--manager"],
                     ["--tui", "--manager", "--now"], ["--tui", "--manager", "a", "--manager", "b"],
                     ["--tui", "--manager=a", "--manager", "b"], ["--tui", "--manager=a", "--manager=b"]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with redirect_stderr(err), mock.patch.object(attended, "layout", side_effect=AssertionError("layout ran")):
                    self.assertEqual(router.main(argv, gql=None, sh=mock.Mock(side_effect=AssertionError("probe ran"))), 2)
                self.assertEqual(err.getvalue(), router.USAGE + "\n")
        self.assertEqual(router.USAGE, "usage: router.py [--now] [--dry-run] [--issue ID] "
                                       "[--tui [--split right|below] [--split-from SESSION] [--events FILE] [--manager NAME]] "
                                       "| --brake")
        self.assertNotIn("--gate", router.USAGE + router.__doc__)
        self.assertIn("--tui [--split right|below] [--split-from SESSION] [--events FILE] [--manager NAME]", router.__doc__)

    def test_manager_is_parsed_with_tui(self):
        for argv, want in ((["--tui"], None), (["--tui", "--manager", "m1"], "m1"), (["--manager=m1", "--tui"], "m1"),
                           (["--issue", "TASK-1", "--tui", "--manager=m-2", "--events", "e"], "m-2")):
            with self.subTest(argv=argv):
                self.assertEqual(router.options(argv)["manager"], want)

    def test_bad_issue_id(self):
        for argv in (["--issue", "task-7"], ["--tui", "--issue", "TASK"], ["--issue", "task-7", "--tui"]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with redirect_stderr(err), mock.patch.object(attended, "layout", side_effect=AssertionError("layout ran")):
                    self.assertEqual(router.main(argv, gql=None, sh=mock.Mock(side_effect=AssertionError("sh ran"))), 2)
                self.assertEqual(err.getvalue(), f"router.py: bad issue id: {argv[argv.index('--issue') + 1]}\n")


class Claim(Base):
    def test_claim_order(self):
        rows = (([issue("TASK-1", "Todo", "researcher", priority=0, created="2026-01-01T00:00:00Z"),
                  issue("TASK-2", "Todo", "researcher", priority=3, created="2026-02-01T00:00:00Z"),
                  issue("TASK-3", "Todo", "researcher", priority=3, created="2026-01-15T00:00:00Z")], ["TASK-3", "TASK-2", "TASK-1"]),
                ([issue("TASK-1", "Todo", "researcher", priority=2, created="2026-09-01T00:00:00Z"),
                  issue("TASK-2", "Todo", "pm", priority=2, created="2026-09-02T00:00:00Z", project=PD),
                  issue("TASK-3", "Todo", "researcher", priority=1, created="2026-09-03T00:00:00Z")], ["TASK-3", "TASK-2", "TASK-1"]),
                ([issue("TASK-1", "Todo", "researcher", priority=2, created="2026-09-01T00:00:00Z"),
                  issue("TASK-2", "Todo", "engineer", priority=2, created="2026-09-03T00:00:00Z"),
                  issue("TASK-3", "Todo", "pm", priority=2, created="2026-09-02T00:00:00Z"),
                  issue("TASK-4", "Todo", "engineer", priority=2, created="2026-09-02T00:00:00Z")], ["TASK-4", "TASK-2", "TASK-3", "TASK-1"]))
        for row, (issues, order) in enumerate(rows):
            with self.subTest(row=row):
                fake = FakeLinear(issues)
                self.assertEqual([self.claim(fake).split()[0] for _ in issues], order)
                self.assertEqual({i["state"] for i in fake.issues.values()}, {"In Progress"})

    def test_queue_only_role_accounts_across_projects(self):
        fake = FakeLinear([issue("TASK-1", "Todo"), issue("TASK-2", "Todo", "someone"), issue("TASK-3", "Todo", "pm", project=None),
                           issue("TASK-4", "Todo", "engineer", project=PD, priority=3)])
        self.assertEqual(self.claim(fake), f"TASK-4 https://linear.app/x/TASK-4 {PD}")
        self.assertEqual([i for i in ("TASK-1", "TASK-2", "TASK-3") if fake.issues[i]["state"] != "Todo"], [])
        flt = next(v["f"] for q, v in fake.queries if "issues(filter" in q)
        self.assertEqual((flt["team"], flt["project"], sorted(flt["assignee"]["id"]["in"])),
                         ({"id": {"eq": TEAM}}, {"null": False}, sorted(f"u-{r}" for r in ROLE)))

    def test_claim_only_moves_to_in_progress(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "pm")])
        self.claim(fake)
        (q, v), = fake.mutations
        self.assertNotIn("assignee", q + repr(v))
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("In Progress", who("pm")))

    def test_full_roles_not_picked(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer", priority=1), issue("TASK-2", "Todo", "researcher", priority=4)])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake, full={"engineer"}), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(self.said(), ["pick TASK-2 queue=1", "claim TASK-2 role=researcher"])
        self.assertEqual((fake.issues["TASK-1"]["state"], "comments" in fake.issues["TASK-1"]), ("Todo", False))
        flt = next(v["f"] for q, v in fake.queries if "issues(filter" in q)
        self.assertEqual(sorted(flt["assignee"]["id"]["in"]), sorted(f"u-{r}" for r in ROLE))

    def test_capped_todo_goes_to_review(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "researcher", priority=2)])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])
        self.assertEqual((fake.issues["TASK-1"]["assignee"], fake.issues["TASK-1"]["subscribers"]), (who("researcher"), ["me@x.com"]))

    def test_capped_todo_reset_by_user(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        hist = {"TASK-1": [{"createdAt": ago(minutes=200), "actorId": USER, "toStateId": STATES["Todo"]}]}
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")], hist)
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "TASK-1 https://linear.app/x/TASK-1 Deep Research")

    def test_no_longer_todo_skipped_for_the_next(self):
        class Snatched(FakeLinear):
            """Someone moves TASK-1 out of Todo right after the Todo list read."""
            def __call__(self, query, **v):
                out = super().__call__(query, **v)
                if "issues(filter" in query and "inverseRelations" in query:
                    self.issues["TASK-1"]["state"] = "In Progress"
                return out

        fake = Snatched([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "researcher", priority=2)])
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(self.said(), ["pick TASK-1 queue=2", "claim-skip TASK-1 reason=no longer Todo",
                                       "pick TASK-2 queue=2", "claim TASK-2 role=researcher"])
        self.assertEqual([v["i"] for _, v in fake.mutations], ["TASK-2"])

    def test_skipped_cap_move_posts_no_comment(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = Moved([issue("TASK-1", "Todo", "researcher")], "In Progress")
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "")
        t = fake.issues["TASK-1"]
        self.assertEqual((t["state"], t["subscribers"]), ("In Progress", ["me@x.com"]))
        self.assertNotIn("comments", t)
        self.assertEqual(writes(fake), [("issueSubscribe", "TASK-1"), ("read", "TASK-1")])
        self.assertEqual(self.said(), ["claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                                       f"move-skip TASK-1 step=pick state={STATES['In Progress']}",
                                       "pick-none reason=nothing claimable"])

    def test_refused_claim_raises(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        fake.refuse.add("TASK-1")
        with self.assertRaises(RuntimeError) as cm:
            self.claim(fake)
        self.assertEqual(str(cm.exception), "issueUpdate: success: false")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")


class Blockers(Base):
    def test_blocked_skipped_for_next_ready(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1, inverse=[
                               blocker("TASK-7"), blocker("TASK-8", "completed"), blocker("TASK-9", "unstarted")]),
                           issue("TASK-2", "Todo", "researcher", priority=2), issue("TASK-3", "Todo", "researcher", priority=3)])
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7", "TASK-9"]', "pick TASK-2 queue=2",
                                       "claim TASK-2 role=researcher"])
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_done_blockers(self):
        for states, blocked in ((["completed"], False), (["canceled"], False), (["duplicate"], False),
                                (["completed", "started"], True)):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker(f"TASK-{n}", s) for n, s in enumerate(states, 7)])])
            self.claim(fake)
            self.assertEqual(fake.issues["TASK-1"]["state"], "Todo" if blocked else "In Progress", states)
            self.assertEqual([m for m in self.said() if m.startswith("blocked ")], ['blocked TASK-1 by=["TASK-8"]'] if blocked else [],
                             states)

    def test_capped_blocked_stays_todo(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")])])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7"]', "pick-none reason=queue empty"])
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("Todo", []))
        self.assertNotIn("comments", fake.issues["TASK-1"])

    def test_only_inverse_blocks_block(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=2,
                                 inverse=[blocker("TASK-5", kind="related"), blocker("TASK-6", kind="duplicate")]),
                           issue("TASK-2", "Todo", "researcher", priority=1, inverse=[blocker("TASK-1", "unstarted")])])
        self.assertEqual(self.claim(fake).split()[0], "TASK-1")
        self.assertEqual(self.said(), ['blocked TASK-2 by=["TASK-1"]', "pick TASK-1 queue=1", "claim TASK-1 role=researcher"])
        self.assertFalse(any(re.search(r"\brelations\b", q) for q, _ in fake.queries))

    def test_null_blocker_unreadable(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker(None), blocker("TASK-7", "completed")])])
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(self.said(), ['blocked TASK-1 by=["(unreadable)"]', "pick-none reason=queue empty"])

    def test_blocker_query_error_reads_per_issue(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1),
                           issue("TASK-2", "Todo", "researcher", priority=2, inverse=[blocker("TASK-7")]),
                           issue("TASK-3", "Todo", "researcher", priority=3, inverse=[blocker("TASK-8", "canceled")])])
        fake.todo_error, fake.unreadable = True, {"TASK-1"}
        self.assertEqual(self.claim(fake), "TASK-3 https://linear.app/x/TASK-3 Deep Research")
        said = self.said()
        self.assertRegex(said[0], r"^todo-error error=SystemExit: linear api error: ")
        self.assertEqual(said[1:], ['blocked TASK-1 by=["(unreadable)"]', 'blocked TASK-2 by=["TASK-7"]', "pick TASK-3 queue=1",
                                    "claim TASK-3 role=researcher"])
        self.assertEqual(fake.reads(), [router.q_issues(router.RELATIONS), router.q_issues()])
        self.assertEqual([v["i"] for q, v in fake.queries if "issue(id:" in q and "inverseRelations" in q], ["TASK-1", "TASK-2", "TASK-3"])

    def test_one_todo_read_with_relations(self):
        for argv in ("claim", "plan", "pick"):
            fake = FakeLinear([issue("TASK-1", "In Progress", "researcher"), issue("TASK-2", "Todo", "researcher")])
            self.plan(fake) if argv == "plan" else self.claim(fake, recover=argv == "pick")
            reads = {s: fake.reads(s) for s in ("Todo", "In Progress")}
            self.assertEqual(len(reads["Todo"]), 1, argv)
            self.assertIn("inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }", reads["Todo"][0], argv)
            self.assertEqual(len(reads["In Progress"]), 0 if argv == "claim" else 1, argv)
            self.assertFalse(any("inverseRelations" in q for q in reads["In Progress"]), argv)
            self.assertFalse(any("issue(id:" in q and "inverseRelations" in q for q, _ in fake.queries), argv)

    def test_dry_run_logs_blocked(self):
        for argv, out, last in (("claim", f"TASK-2 https://linear.app/x/TASK-2 {PD}", ["pick TASK-2 queue=1"]),
                                ("plan", "new", [])):  # next_run logs no plan: the tick does
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7"), blocker("TASK-8", "backlog")]),
                               issue("TASK-2", "Todo", "pm", project=PD)])
            self.assertEqual(self.plan(fake, dry=True) if argv == "plan" else self.claim(fake, dry=True), out, argv)
            self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7", "TASK-8"]', *last], argv)
            self.assertEqual(fake.mutations, [], argv)


def outer_args(ident, project, assignee, sid, task, mode, runner="headless", split=None, split_from=None, events=None,
               manager=None):
    """The outer's arguments the router passes."""
    return argparse.Namespace(issue=ident, project=project, assignee=assignee, sid=sid, task=task, mode=mode, runner=runner,
                              split=split, split_from=split_from, events=events, manager=manager)


class FakeShell:
    """sessions: the tmux session names list-sessions prints; none = no tmux server (exit 1). fives: five_hour of the first
    probes, then probe_five. start, the fake outer, records ("launch", its arguments) in calls; it raises raises.get(ID),
    else exits exits.get(ID, 0); exit 0 adds agent-pm-<role>-<ID> (role from the assignee) unless ID is in bounce. own: the
    session name display-message prints."""
    def __init__(self, sessions=(), probe_five=0.2, fives=(), exits=None, bounce=(), raises=None, own="mgr"):
        self.calls, self.five, self.sessions, self.own = [], probe_five, list(sessions), own
        self.fives, self.exits, self.bounce, self.raises = list(fives), exits or {}, set(bounce), raises or {}

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd == LIST:
            return subprocess.CompletedProcess(cmd, 0 if self.sessions else 1, stdout="".join(f"{n}\n" for n in self.sessions))
        if cmd[:2] == ["tmux", "display-message"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{self.own}\n")
        if cmd[0] == "claude":
            five = self.fives.pop(0) if self.fives else self.five
            ev = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": five}, "seven_day": {"utilization": 0.1}}}}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(ev) + "\n")
        return subprocess.CompletedProcess(cmd, 0)

    def start(self, a):
        self.calls.append(("launch", a))
        if a.issue in self.raises:
            raise self.raises[a.issue]
        rc = self.exits.get(a.issue, 0)
        if not rc and a.issue not in self.bounce:
            self.sessions.append(config.session(next(r for r, e in ROLE.items() if e == a.assignee), a.issue))
        return rc

    def launches(self):
        return [c[1] for c in self.calls if c[0] == "launch"]


@contextlib.contextmanager
def other_router(runs):
    """<runs' dir>/router.lock held as another router holds it (flock conflicts across open files in one process too)."""
    os.makedirs(os.path.dirname(runs), exist_ok=True)
    with open(os.path.join(os.path.dirname(runs), "router.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class Tick(Base):
    def test_hours_boundaries(self):
        for hour, runs in ((0, False), (1, True), (6, True), (7, False), (12, False), (23, False)):
            with self.subTest(hour=hour):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, hour=hour), 0)
                self.assertEqual(len(self.sh.launches()), int(runs))
                self.assertEqual(self.said() == [], not runs)
                self.assertEqual(self.sh.calls == [], not runs)

    def test_launchd_path_replaced(self):
        self.tick(FakeLinear([]))
        self.assertEqual(self.path, router.PATH)
        self.assertIn("/opt/homebrew/bin", self.path)

    def test_now_skips_hours_and_starts(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, "--now", hour=12)
        (launch,) = self.sh.launches()
        sid = launch.sid
        self.assertEqual(router.RUN, os.path.join(config.ROOT, "orchestrator", "src", "run.py"))
        self.assertEqual(launch, outer_args("TASK-1", IDS[DR], ROLE["researcher"], sid, None, "new"))
        self.assertEqual(self.last_run(), ("start", "TASK-1", sid, "researcher"))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_begin_appends_one_json_line_to_runs_jsonl(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with mock.patch.object(drive, "stamp", return_value="2026-10-09T00:26:33-04:00"):
            self.tick(fake, "--now", hour=12)
        (launch,) = self.sh.launches()
        self.assertEqual(self.state, '{"ts": "2026-10-09T00:26:33-04:00", "kind": "start", "issue": "TASK-1", '
                                     f'"sid": "{launch.sid}", "role": "researcher"}}\n')
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["cfg", "router.lock", "runs.jsonl", "transcripts"])

    def test_an_idle_tick_writes_no_line(self):
        """FR-10: outside hours, all roles full, nothing to do beside a live session; a blocked issue once a day."""
        full = ["agent-pm-researcher-TASK-5", "agent-pm-pm-TASK-6", "agent-pm-engineer-TASK-7"]
        cases = [(12, FakeShell(), [issue("TASK-1", "Todo", "researcher")]),
                 (2, FakeShell(full), [issue("TASK-1", "Todo", "researcher")]),
                 (2, FakeShell(["agent-pm-engineer-TASK-2"]), [issue("TASK-2", "In Progress", "engineer")])]
        for hour, shell, issues in cases:
            with self.subTest(hour=hour, live=shell.sessions):
                self.assertEqual(self.tick(FakeLinear(issues), hour=hour, shell=shell), 0)
                self.assertEqual((self.said(), self.err, os.listdir(self.logs)), ([], "", []))
        blocked = [issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")])]
        self.tick(FakeLinear(blocked))
        self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7"]'])
        self.tick(FakeLinear(blocked), again=True)
        self.assertEqual(self.said(), [])

    def test_live_sessions_exact_names(self):
        sh = FakeShell(["agent-pm-engineer-TASK-2", "agent-pm-engineer-TASK-10", "agent-pm-researcher-TASK-1", "agent-pm-pm-TASK-6",
                        "agent-pm-pm-lead-TASK-4", "agent-pm-engineer", "agent-pm-engineer-TASK-1x", "agent-pm-engineer-task-3",
                        "agent-pm-engineer-TASK-", "agent-pm-ghost-TASK-5", "other"])
        self.assertEqual(router.live_sessions(["researcher", "pm", "pm-lead", "engineer"], sh),
                         {"researcher": ["TASK-1"], "pm": ["TASK-6"], "pm-lead": ["TASK-4"], "engineer": ["TASK-10", "TASK-2"]})
        self.assertEqual(sh.calls, [LIST])
        self.tick(FakeLinear([]))
        self.assertEqual([c for c in self.sh.calls if c[0] == "tmux"], [LIST])

    def test_live_sessions_no_server(self):
        calls = []

        def sh(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 1, stdout="agent-pm-engineer-TASK-1\n", stderr="no server running")
        self.assertEqual(router.live_sessions(["researcher", "engineer"], sh), {"researcher": [], "engineer": []})
        self.assertEqual(calls, [(LIST, {"capture_output": True, "text": True})])

    def test_all_full_skips_before_linear(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        for cfg, live in ((CONFIG, ["agent-pm-researcher-TASK-5", "agent-pm-pm-TASK-6", "agent-pm-engineer-TASK-7"]),
                          (CONFIG_ENG2, ["agent-pm-researcher-TASK-5", "agent-pm-pm-TASK-6", "agent-pm-engineer-TASK-7",
                                         "agent-pm-engineer-TASK-9"])):
            self.config = self.write_config(cfg)
            counts = json.dumps({r: f"{n}/{n}" for r, n in (("engineer", len(live) - 2), ("pm", 1), ("researcher", 1))})
            for argv in ((), ("--dry-run",), ("--now",)):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv, shell=FakeShell(live)), 0)
                self.assertEqual(self.said(), [f"skip reason=all roles full live={counts}"] if "--dry-run" in argv else [],
                                 (cfg, argv))
                self.assertEqual(fake.queries, [], argv)
                self.assertEqual(self.sh.calls, [LIST], argv)
                self.assertIn("TASK-8", self.state, argv)

    def test_partly_full_role_gets_a_launch(self):
        self.config = self.write_config(CONFIG_ENG2)
        fake = FakeLinear([issue("TASK-1", "In Progress", "engineer", priority=1, project=PD),
                           issue("TASK-2", "Todo", "engineer", priority=2, project=PD),
                           issue("TASK-3", "Todo", "engineer", priority=3, project=PD)], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.tick(fake, shell=FakeShell(["agent-pm-engineer-TASK-1"]))
        self.assertEqual(self.said(), ['plan mode=new queue=2 live={"engineer": "1/2"}', "pick TASK-2 queue=2",
                                       "claim TASK-2 role=engineer", "launch TASK-2 exit=0"])
        self.assertEqual(self.launched_runs(), [("TASK-2", "new")])
        self.assertEqual({i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
                         {"TASK-1": ("In Progress", None), "TASK-2": ("In Progress", None), "TASK-3": ("Todo", None)})

    def test_todo_issue_with_live_session_not_claimed(self):
        self.config = self.write_config(CONFIG_ENG2)
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer", priority=1, project=PD),
                           issue("TASK-2", "Todo", "engineer", priority=2, project=PD)])
        self.tick(fake, shell=FakeShell(["agent-pm-engineer-TASK-1"]))
        self.assertEqual(self.said(), ['plan mode=new queue=1 live={"engineer": "1/2"}', "pick TASK-2 queue=1",
                                       "claim TASK-2 role=engineer", "launch TASK-2 exit=0"])
        self.assertEqual({i: t["state"] for i, t in fake.issues.items()}, {"TASK-1": "Todo", "TASK-2": "In Progress"})

    def test_full_role_todo_not_claimed_and_non_live_in_progress_recovered(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "engineer", priority=1, updated=ago(hours=3)),
                           issue("TASK-2", "In Progress", "engineer", updated=ago(hours=3)),
                           issue("TASK-3", "In Progress", "engineer"),
                           issue("TASK-4", "Todo", "engineer", priority=1), issue("TASK-5", "Todo", "pm", priority=3, project=PD),
                           issue("TASK-6", "Todo", "engineer", priority=1)], self.hist)
        for ident in ("TASK-1", "TASK-3", "TASK-4"):
            for m in (390, 380, 370, 360):
                self.add("start", ident, f"{ident}-{m}", m)
        self.tick(fake, shell=FakeShell(["agent-pm-engineer-TASK-1"]))
        self.assertEqual(self.said(), ["recover TASK-3 to=in_review reason=reached 4 attempts",
                                       f"recover TASK-2 to=todo reason=no session updated={ago(hours=3)}",
                                       'plan mode=new queue=1 live={"engineer": "1/1"}', "pick TASK-5 queue=1",
                                       "claim TASK-5 role=pm", "launch TASK-5 exit=0"])
        self.assertEqual({i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
                         {"TASK-1": ("In Progress", None), "TASK-2": ("Todo", [router.INTERRUPTED]),
                          "TASK-3": ("In Review", [router.CAP_COMMENT]), "TASK-4": ("Todo", None), "TASK-5": ("In Progress", None),
                          "TASK-6": ("Todo", None)})
        self.assertEqual(self.launched(), "TASK-5")
        self.assertNotIn("TASK-1", [v["i"] for q, v in fake.queries if "history" in q])
        flts = [v["f"] for q, v in fake.queries if "issues(filter" in q]
        self.assertEqual({tuple(sorted(f["assignee"]["id"]["in"])) for f in flts}, {tuple(sorted(f"u-{r}" for r in ROLE))})

    def test_full_role_resume_not_launched(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "pm", priority=1, project=PD),
                           issue("TASK-2", "In Progress", "researcher", priority=3), issue("TASK-3", "Todo", "engineer", priority=1)], self.hist)
        self.resumable("TASK-1", "a", 120, "pm")
        self.resumable("TASK-2", "b", 60)
        self.tick(fake, shell=FakeShell(["agent-pm-pm-TASK-9"]))
        self.assertEqual(self.said()[0], 'plan TASK-2 mode=resume sid=b live={"pm": "1/1"}')
        self.assertEqual(self.launched_runs(), [("TASK-2", "resume")])
        self.assertEqual({i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
                         {"TASK-1": ("In Progress", None), "TASK-2": ("In Progress", None), "TASK-3": ("Todo", None)})
        self.assertNotIn("resume TASK-1", self.state)

    def test_nothing_to_do_with_full_role(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "In Progress", "engineer", updated=ago(hours=3))])
        self.tick(fake, shell=FakeShell(["agent-pm-engineer-TASK-2"]))
        self.assertEqual(self.said(), [])
        self.assertEqual(self.sh.calls, [LIST])
        self.assertEqual([v["e"] for q, v in fake.queries if "users(filter" in q], [ROLE["researcher"], ROLE["pm"], ROLE["engineer"]])
        self.assertEqual(fake.mutations, [])

    def test_one_run_per_tick_resume_first(self):
        self.config = self.write_config(CONFIG_ENG2)
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", priority=3),
                           issue("TASK-8", "In Progress", "engineer", priority=4, project=PD),
                           issue("TASK-2", "Todo", "researcher", priority=1), issue("TASK-3", "Todo", "pm", priority=2, project=PD),
                           issue("TASK-4", "Todo", "engineer", priority=2, project=PD)], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.resumable("TASK-8", "h", 70, "engineer")
        self.tick(fake)
        self.assertEqual(self.said(), ["plan TASK-1 mode=resume sid=a", "launch TASK-1 exit=0"])
        self.assertEqual(self.launched_runs(), [("TASK-1", "resume")])
        self.assertEqual({i: t["state"] for i, t in fake.issues.items()},
                         {"TASK-1": "In Progress", "TASK-8": "In Progress", "TASK-2": "Todo", "TASK-3": "Todo", "TASK-4": "Todo"})
        self.assertEqual((self.probes(), self.sh.calls.count(LIST)), (1, 1))

    def test_one_claim_per_tick(self):
        self.config = self.write_config(CONFIG_ENG2)
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "pm", priority=2, project=PD),
                           issue("TASK-3", "Todo", "engineer", priority=2, project=PD)])
        self.tick(fake)
        self.assertEqual(self.said(), ["plan mode=new queue=3", "pick TASK-1 queue=3", "claim TASK-1 role=researcher",
                                       "launch TASK-1 exit=0"])
        self.assertEqual([c[1].issue if c[0] == "launch" else c[0] for c in self.sh.calls], ["tmux", "claude", "TASK-1"])
        self.assertEqual({i: t["state"] for i, t in fake.issues.items()}, {"TASK-1": "In Progress", "TASK-2": "Todo", "TASK-3": "Todo"})

    def test_dry_run_plans_one_run(self):
        self.config = self.write_config(CONFIG_ENG2)
        fake = FakeLinear([issue("TASK-3", "Todo", "pm", priority=1, project=PD), issue("TASK-4", "Todo", "pm", priority=3, project=PD),
                           issue("TASK-5", "Todo", "engineer", priority=2, project=PD),
                           issue("TASK-6", "Todo", "engineer", priority=3, project=PD)], self.hist)
        for sid in "wxyz":
            self.add("start", "TASK-3", sid, 300)
        before = self.unmap("\n".join(self.lines) + "\n")
        self.tick(fake, "--dry-run")
        said = self.said()
        self.assertEqual(said[:-1], ["plan mode=new queue=4", "claim-skip TASK-3 to=in_review reason=reached 4 attempts",
                                     "pick TASK-5 queue=4"])
        self.assertRegex(said[-1], r"^usage usage=status=allowed .* planned=true allowed=true$")
        self.assertEqual(self.sh.calls, [LIST, router.PROBE])
        self.assertEqual((fake.mutations, self.state), ([], before))
        self.assertNotIn(RECHECK, [q for q, _ in fake.queries])

    def test_dry_run_usage_blocked(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, "--dry-run", shell=FakeShell(probe_five=0.95))
        self.assertEqual(self.said()[:-1], ["plan mode=new queue=1", "pick TASK-1 queue=1"])
        self.assertRegex(self.said()[-1], r"^usage usage=status=allowed five_hour=0.95 .* planned=true allowed=false$")

    def test_dry_run(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "Todo", "researcher")])
        self.tick(fake, "--dry-run", hour=12, shell=FakeShell(["agent-pm-pm-TASK-5", "agent-pm-engineer-TASK-6"]))
        said = self.said()
        self.assertEqual(said[:3], ["skip reason=outside hours", 'plan mode=new queue=1 live={"engineer": "1/1", "pm": "1/1"}',
                                    "pick TASK-2 queue=1"])
        self.assertRegex(said[3], r"^usage usage=status=allowed .* planned=true allowed=true$")
        self.assertEqual(len(said), 4)
        self.assertEqual(self.probes(), 1)
        self.assertEqual((self.sh.launches(), fake.mutations), ([], []))
        self.assertIn("TASK-8", self.state)

    def test_all_todo_blocked_skips_probe(self):
        def issues():
            return [issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-2", "unstarted")]),
                    issue("TASK-2", "Todo", "pm", project=PD, inverse=[blocker("TASK-1", "unstarted")]),
                    issue("TASK-3", "Todo", "engineer", inverse=[blocker("TASK-9", "started")])]

        fake = FakeLinear(issues())
        self.assertEqual(self.tick(fake), 0)
        blocked = ['blocked TASK-1 by=["TASK-2"]', 'blocked TASK-2 by=["TASK-1"]', 'blocked TASK-3 by=["TASK-9"]']
        self.assertEqual(self.said(), blocked)
        self.assertEqual(([c[0] for c in self.sh.calls].count("claude"), self.sh.launches(), fake.mutations), (0, [], []))
        self.assertEqual(len(fake.reads()), 1)
        fake = FakeLinear(issues())
        self.assertEqual(self.tick(fake, "--dry-run"), 0)
        self.assertEqual(self.said()[:3], blocked)
        self.assertRegex(self.said()[3], r"^usage usage=.* planned=false allowed=true$")
        self.assertEqual(len(self.said()), 4)
        self.assertEqual(([c[0] for c in self.sh.calls].count("claude"), self.sh.launches(), fake.mutations), (1, [], []))

    def test_ready_tick_reads_todo_once(self):
        for argv, runs in (((), [("TASK-2", "new")]), (("--issue", "TASK-2"), [("TASK-2", "new")])):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1, inverse=[blocker("TASK-7")]),
                               issue("TASK-2", "Todo", "researcher", priority=2), issue("TASK-3", "Todo", "pm", priority=3, project=PD)])
            self.assertEqual(self.tick(fake, *argv), 0, argv)
            self.assertEqual(self.launched_runs(), runs, argv)
            self.assertEqual(len(fake.reads()), 1, argv)
            self.assertEqual("plan mode=new queue=2" in self.said(), not argv, argv)
            self.assertEqual(self.said().count('blocked TASK-1 by=["TASK-7"]'), 1, argv)

    def test_usage_blocked(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, shell=FakeShell(probe_five=0.95))
        (said,) = self.said()
        self.assertRegex(said, r"^usage-skip mode=new reason=blocked by usage usage=status=allowed five_hour=0.95 ")
        self.assertEqual(self.sh.launches(), [])
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")
        self.tick(fake, shell=FakeShell(probe_five=0.97), again=True)
        self.assertEqual(self.said(), [])

    def test_prune_runs_before_plan(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        self.tick(FakeLinear([]))
        self.assertNotIn("TASK-8", self.state)

    def test_prune_failure_continues(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with mock.patch.object(router, "prune", side_effect=OSError("disk")):
            self.tick(fake)
        self.assertIn("skip reason=prune failed error=OSError: disk", self.said())
        self.assertEqual(len(self.sh.launches()), 1)

    def test_the_outers_exit_and_exceptions_never_break_the_tick(self):
        cases = [(FakeShell(exits={"TASK-1": 3}), "launch TASK-1 exit=3"),
                 (FakeShell(raises={"TASK-1": SystemExit("linear api error: boom")}),
                  "launch-error TASK-1 error=SystemExit: linear api error: boom"),
                 (FakeShell(raises={"TASK-1": RuntimeError("x")}), "launch-error TASK-1 error=RuntimeError: x")]
        for shell, end in cases:
            with self.subTest(end=end):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, shell=shell), 0)
                self.assertEqual(self.said()[-1], end)
                (launch,) = self.sh.launches()
                self.assertEqual(self.last_run(), ("start", "TASK-1", launch.sid, "researcher"))

    def test_resume_finds_the_transcript_under_the_recorded_cwd(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")], self.hist)
        self.moved("TASK-1", 61)
        self.add("start", "TASK-1", "a", 60)
        os.makedirs(config.run_dir("TASK-1"), exist_ok=True)
        with open(os.path.join(config.run_dir("TASK-1"), "run.jsonl"), "w") as f:
            f.write(json.dumps({"ts": "t", "kind": "session", "sid": self.sid("a"), "cwd": self.tmp.name,
                                "project": False}) + "\n")
        self.addCleanup(os.remove, os.path.join(config.run_dir("TASK-1"), "run.jsonl"))
        self.touch("TASK-1", "a", 40)
        self.assertTrue(config.transcript("TASK-1", self.sid("a"), self.tdir).startswith(
            os.path.join(self.tdir, re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(self.tmp.name)))))
        self.assertEqual(self.plan(fake), f"resume TASK-1 a {fake.issues['TASK-1']['url']} {DR}")

    def test_driver_session_counts_toward_max_runs_and_a_tui_session_does_not(self):
        tui = drive.tui_session("engineer", self.sid("a"))
        for live, runs in (([tui, "agent-pm-engineer-TASK-9"], []), ([tui], [("TASK-1", "new")])):
            with self.subTest(live=live):
                self.tick(FakeLinear([issue("TASK-1", "Todo", "engineer")]), shell=FakeShell(live))
                self.assertEqual(self.launched_runs(), runs)
        self.assertEqual(router.live_sessions(["engineer"], FakeShell([tui, "agent-pm-engineer-TASK-9"])), {"engineer": ["TASK-9"]})


class IssueFlag(Base):
    """router.py --issue ID: only that issue, without the hours, max_runs and usage gates; the outer faked."""
    def test_todo_is_claimed_and_started_whatever_the_gates(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)),
                         issue("TASK-2", "In Progress", "researcher", priority=1), issue("TASK-3", "Todo", "researcher", priority=1),
                         issue("TASK-4", "Todo", "engineer", priority=3, project=PD))
        self.resumable("TASK-2", "b", 60)
        full = FakeShell(["agent-pm-researcher-TASK-5", "agent-pm-pm-TASK-6", "agent-pm-engineer-TASK-7"], probe_five=0.95)
        self.assertEqual(self.tick(fake, "--issue", "TASK-4", hour=12, shell=full), 0)
        self.assertEqual(self.said(), ["pick TASK-4 queue=1", "claim TASK-4 role=engineer"])
        (launch,) = self.sh.launches()
        self.assertEqual(launch, outer_args("TASK-4", IDS[PD], ROLE["engineer"], launch.sid, None, "new"))
        self.assertRegex(launch.sid, config.UUID_RE)
        self.assertEqual(self.last_run(), ("start", "TASK-4", launch.sid, "engineer"))
        self.assertIn("TASK-8", self.state)
        self.assertEqual(self.sh.calls, [LIST, ("launch", launch)])
        self.assertEqual({i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
                         {"TASK-1": ("In Progress", None), "TASK-2": ("In Progress", None), "TASK-3": ("Todo", None),
                          "TASK-4": ("In Progress", None)})
        self.assertEqual(len(fake.reads()), 1)

    def test_exits_with_the_outers_code(self):
        for rc in (0, 1, 3):
            with self.subTest(rc=rc):
                self.assertEqual(self.tick(FakeLinear([issue("TASK-1", "Todo", "researcher")]), "--issue", "TASK-1",
                                           shell=FakeShell(exits={"TASK-1": rc})), rc)

    def test_a_live_agent_run_is_refused_with_its_attach_command(self):
        for state in ("Todo", "In Progress", "In Review"):
            for dry in (False, True):
                with self.subTest(state=state, dry=dry):
                    fake = FakeLinear([issue("TASK-1", state, "engineer")])
                    argv = ("--issue", "TASK-1", *["--dry-run"] * dry)
                    self.assertEqual(self.tick(fake, *argv, shell=FakeShell(["agent-pm-engineer-TASK-1"])), 0 if dry else 1)
                    self.assertEqual(self.err, "router.py: TASK-1 has a live agent run: tmux attach -t '=agent-pm-engineer-TASK-1'\n")
                    self.assertEqual((fake.queries, self.sh.calls, self.state), ([], [LIST], ""))

    def test_in_progress_resumes_inside_recovers_time_windows(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.resumable("TASK-1", "a", 20)
        self.touch("TASK-1", "a", 5)
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(self.tick(fake, "--issue", "TASK-1", hour=12), 0)
        self.assertEqual(self.said(), ["plan TASK-1 mode=resume sid=a"])
        (launch,) = self.sh.launches()
        self.assertEqual(launch, outer_args("TASK-1", IDS[DR], ROLE["researcher"], self.sid("a"), None, "resume"))
        self.assertEqual(self.last_run(), ("resume", "TASK-1", "a", "researcher"))
        self.assertEqual((fake.mutations, self.probes()), ([], 0))

    def nothing_resumable(self):
        """(name, issue, runs.jsonl lines and transcripts, said, state, comment) per kind of In Progress issue Recover
        settles without resuming; the last two inside Recover's time windows."""
        def capped():
            for m in (390, 380, 370, 360):
                self.add("start", "TASK-1", f"s{m}", m)
        return [("cap", issue("TASK-1", "In Progress", "researcher"), capped,
                 "recover TASK-1 to=in_review reason=reached 4 attempts", "In Review", router.CAP_COMMENT),
                ("role", issue("TASK-1", "In Progress", "researcher"), lambda: self.resumable("TASK-1", "a", 60, "engineer"),
                 "recover TASK-1 to=in_review reason=role (engineer) is not the assignee's (researcher)", "In Review",
                 "The interrupted agent run's role (engineer) is not the assignee's (researcher); needs a look."),
                ("no transcript", issue("TASK-1", "In Progress", "researcher"), lambda: self.add("start", "TASK-1", "n", 5),
                 "recover TASK-1 to=todo reason=no transcript sid=n", "Todo", router.INTERRUPTED),
                ("no sid", issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=5)), lambda: None,
                 f"recover TASK-1 to=todo reason=no session updated={ago(minutes=5)}", "Todo", router.INTERRUPTED)]

    def test_in_progress_with_nothing_to_resume_is_settled_at_once(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        for name, todo, given, said, state, comment in self.nothing_resumable():
            with self.subTest(name=name):
                self.lines, self.hist = [], {}
                given()
                fake = self.fake(todo)
                before = self.unmap("\n".join(self.lines) + ("\n" if self.lines else ""))
                if name in ("no transcript", "no sid"):
                    self.assertEqual((self.plan(fake), fake.mutations), ("", []))
                self.assertEqual(self.tick(fake, "--issue", "TASK-1"), 1)
                self.assertEqual(self.said(), [said])
                self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["comments"]), (state, [comment]))
                self.assertEqual((self.sh.launches(), self.probes(), self.state), ([], 0, before))

    def test_dry_run_prints_the_decision_only(self):
        def todo():
            return self.fake(issue("TASK-1", "Todo", "researcher"))

        def resumable():
            self.resumable("TASK-1", "a", 20)
            return self.fake(issue("TASK-1", "In Progress", "researcher"))
        cases = [("todo", todo, ["pick TASK-1 queue=1"]), ("resume", resumable, ["plan TASK-1 mode=resume sid=a"])]
        cases += [(name, lambda ip=ip, given=given: (given(), self.fake(ip))[1], [said])
                  for name, ip, given, said, _, _ in self.nothing_resumable()]
        for name, given, said in cases:
            with self.subTest(name=name):
                self.lines, self.hist = [], {}
                fake = given()
                before = self.unmap("\n".join(self.lines) + ("\n" if self.lines else ""))
                self.assertEqual(self.tick(fake, "--dry-run", "--issue", "TASK-1"), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("Todo" if name == "todo" else "In Progress", []))
                self.assertEqual((self.sh.launches(), self.probes(), self.state), ([], 0, before))
                self.assertNotIn(RECHECK, [q for q, _ in fake.queries])

    def test_todo_not_claimable_starts_nothing(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        for argv in (("--issue", "TASK-1"), ("--issue", "TASK-1", "--dry-run")):
            with self.subTest(argv=argv):
                self.lines = []
                for sid in "abcd":
                    self.add("start", "TASK-1", sid, 300)
                before = self.unmap("\n".join(self.lines) + "\n")
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv), 0 if "--dry-run" in argv else 1)
                self.assertEqual(self.said(), ["claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                                               "pick-none reason=nothing claimable"])
                self.assertEqual(fake.issues["TASK-1"]["state"], "Todo" if "--dry-run" in argv else "In Review")
                self.assertEqual((self.sh.launches(), self.state), ([], before))

    def test_blocked_starts_nothing(self):
        for dry in (False, True):
            for resume in (False, True):
                with self.subTest(dry=dry, resume=resume):
                    self.lines, self.hist = [], {}
                    issues = [issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")]), issue("TASK-4", "Todo", "pm", project=PD)]
                    if resume:
                        issues.append(issue("TASK-2", "In Progress", "researcher"))
                        self.resumable("TASK-2", "a", 60)
                    fake = FakeLinear(issues, self.hist)
                    before = self.unmap("\n".join(self.lines) + ("\n" if self.lines else ""))
                    self.assertEqual(self.tick(fake, "--issue", "TASK-1", *["--dry-run"] * dry), 0 if dry else 1)
                    self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7"]'])
                    self.assertEqual((self.probes(), self.sh.launches(), fake.mutations, self.state), (0, [], [], before))
                    self.assertEqual(len(fake.reads()), 1)

    def test_another_state_or_not_a_role_account_starts_nothing(self):
        fake = FakeLinear([issue("TASK-1", "In Review", "researcher"), issue("TASK-2", "Todo", "someone"), issue("TASK-3", "Todo"),
                           issue("TASK-4", "In Progress", "someone", updated=ago(hours=3)),
                           issue("TASK-5", "Todo", "researcher", project=None)])
        for ident in ("TASK-1", "TASK-2", "TASK-3", "TASK-4", "TASK-5", "TASK-9"):
            with self.subTest(ident=ident):
                self.assertEqual(self.tick(fake, "--issue", ident), 1)
                self.assertEqual(self.said(), [f"pick-none {ident} reason=not a Todo issue assigned to a role account"])
                self.assertEqual((self.sh.launches(), fake.mutations, self.state), ([], [], ""))


class OneRouter(Base):
    """A router finding router.lock held does nothing else; once it is free, the same call goes on."""
    def test_a_tick_skips_while_another_router_runs(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        before = self.unmap("\n".join(self.lines) + "\n")
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with other_router(self.log):
            self.assertEqual(self.tick(fake), 0)
            self.assertEqual(self.said(), ["skip reason=another router is running"])
            self.assertEqual((fake.queries, self.sh.calls, self.state, fake.issues["TASK-1"]["state"]), ([], [], before, "Todo"))
            self.assertEqual((self.tick(fake, hour=12), self.said(), self.sh.calls), (0, [], []))  # outside hours: no lock
        self.assertEqual(self.tick(fake), 0)
        self.assertEqual(self.launched_runs(), [("TASK-1", "new")])
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_a_dry_run_and_the_brake_take_no_lock(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with other_router(self.log):
            self.assertEqual(self.tick(fake, "--dry-run"), 0)
            self.assertEqual(self.said()[:-1], ["plan mode=new queue=1", "pick TASK-1 queue=1"])
            self.assertRegex(self.said()[-1], "^usage ")
            self.assertEqual(self.tick(fake, "--dry-run", "--issue", "TASK-1"), 0)
            self.assertEqual(self.said(), ["pick TASK-1 queue=1"])
            out = io.StringIO()
            with redirect_stdout(out), mock.patch.dict(os.environ):
                self.assertEqual(router.main(["--brake"], runs=self.log, sh=FakeShell()), 0)
            self.assertRegex(out.getvalue(), "^status=allowed ")
        self.assertEqual(fake.mutations, [])


class LostClaim(FakeLinear):
    """The first state change is applied (updatedAt NOW), its answer lost: Unavailable."""
    def __init__(self, issues):
        super().__init__(issues)
        self.lose = True

    def __call__(self, query, **v):
        out = super().__call__(query, **v)
        if query == linear.M_STATE and self.lose:
            self.lose = False
            self.issues[v["i"]]["updatedAt"] = ago(minutes=0)
            raise linear.Unavailable("issueUpdate", reason="TimeoutError: timed out")
        return out


class Outage(Base):
    """Linear unavailable: the router stops at the first failure, one linear-error a day, exit 1, the lock released."""
    SAID = {name: f"linear-error op=teams {fields}" for name, fields in FIELDS.items()}

    def test_a_failure_at_board_setup_is_one_linear_error_a_day(self):
        for argv in (("--now",), ("--issue", "TASK-1")):
            for name, error in FAILURES.items():
                with self.subTest(argv=argv, failure=name):
                    for again in (False, True):
                        gql = failing(error)
                        self.assertEqual(self.tick(gql, *argv, again=again), 1)
                        self.assertEqual((self.said(), self.raw), ([] if again else [self.SAID[name]], ""))
                        self.assertEqual(gql.failed, [(linear.Q_TEAM, {"t": TEAM})])
                        self.assertEqual((self.sh.launches(), self.state), ([], ""))
                    fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                    self.assertEqual(self.tick(fake, *argv), 0)
                    self.assertEqual(self.launched_runs(), [("TASK-1", "new")])

    def test_a_dry_run_prints_it_and_writes_nothing(self):
        for argv in (("--now", "--dry-run"), ("--issue", "TASK-1", "--dry-run")):
            for name, error in FAILURES.items():
                with self.subTest(argv=argv, failure=name):
                    self.assertEqual(self.tick(failing(error), *argv), 1)
                    self.assertEqual((self.said(), len(self.raw.splitlines())), ([self.SAID[name]], 1))
                    self.assertEqual((os.listdir(self.logs), self.state), ([], ""))

    def test_a_claim_applied_with_its_answer_lost_is_recovered_once_stale(self):
        for argv, plan in ((("--now",), ["plan mode=new queue=1"]), (("--issue", "TASK-1"), [])):
            with self.subTest(argv=argv):
                fake = LostClaim([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv), 1)
                self.assertEqual(self.said(), plan + ["pick TASK-1 queue=1", "claim TASK-1 role=researcher",
                                                      "linear-error op=issueUpdate reason=TimeoutError: timed out"])
                self.assertEqual((fake.issues["TASK-1"]["state"], self.sh.launches(), self.state), ("In Progress", [], ""))
                self.assertEqual(self.tick(fake, "--now", now=NOW + router.STALE + timedelta(minutes=1)), 0)
                self.assertEqual(self.said(), [f"recover TASK-1 to=todo reason=no session updated={ago(minutes=0)}",
                                               "plan mode=new queue=1", "pick TASK-1 queue=1", "claim TASK-1 role=researcher",
                                               "launch TASK-1 exit=0"])
                self.assertEqual(fake.issues["TASK-1"]["comments"], [router.INTERRUPTED])


HINT = "router.py: no manager directory (not in tmux): this run writes no events; give --manager <name>"
DISPLAY = ["tmux", "display-message", "-p", "-t", "%3", "#{session_name}"]


class TuiTick(Base):
    """router.py --tui: attended.layout (patched) and the events file before the tick or --issue; the outer gets the tui
    runner. Outside tmux and on a HOME of its own unless a test says otherwise (in_tmux)."""
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ))
        self.fresh(False)

    def fresh(self, tmux):
        """A new HOME (so a new ~/.agent-pm), in tmux or not."""
        self.agent_pm = hermetic.home(self)
        self.managers = os.path.join(self.agent_pm, "managers")
        for k in ("TMUX", "TMUX_PANE"):
            os.environ.pop(k, None)
        if tmux:
            self.in_tmux()

    def in_tmux(self, pane="%3"):
        os.environ.update(TMUX="/tmp/tmux-501/default,1,0", TMUX_PANE=pane)

    def events_of(self, name):
        return os.path.join(self.managers, name, "events")

    def mode(self, path):
        return os.stat(path).st_mode & 0o777

    def tui_tick(self, fake, *argv, layout=None, **kw):
        place = mock.Mock(side_effect=layout, return_value=drive.Layout())
        with mock.patch.object(attended, "layout", place):
            rc = self.tick(fake, *argv, **kw)
        self.layout = place.call_args_list
        return rc

    def test_new_run_launch_argv(self):
        events = os.path.join(self.tmp.name, "events.log")
        rows = ((("--now", "--tui"), None, None, None),
                (("--tui", "--split-from", "dev", "--now", "--split", "below"), "below", "dev", None),
                (("--split-from", "-x", "--tui"), None, "-x", None),
                (("--split", "right", "--tui"), "right", None, None),
                (("--now", "--tui", "--split=below", "--split-from=dev"), "below", "dev", None),
                (("--split-from=dev", "--tui", "--split", "right"), "right", "dev", None),
                (("--tui", "--events", events), None, None, events),
                (("--tui", f"--events={events}", "--split", "below"), "below", None, events),
                (("--issue", "TASK-1", "--tui"), None, None, None),
                (("--tui", "--split", "below", "--issue", "TASK-1", "--split-from", "dev", "--events", events),
                 "below", "dev", events))
        for argv, split, split_from, ev in rows:
            with self.subTest(argv=argv):
                self.lines = []
                if os.path.exists(events):
                    os.remove(events)
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tui_tick(fake, *argv), 0)
                (launch,) = self.sh.launches()
                self.assertEqual(launch, outer_args("TASK-1", IDS[DR], ROLE["researcher"], launch.sid, None, "new",
                                                    "tui", split, split_from, ev))
                self.assertEqual(self.last_run(), ("start", "TASK-1", launch.sid, "researcher"))
                self.assertEqual(self.layout, [mock.call(split, split_from)])
                self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")
                self.assertEqual(os.path.exists(events), ev is not None)

    def test_resume_launch_argv(self):
        for argv in (("--tui", "--split", "below"), ("--issue", "TASK-1", "--tui", "--split", "below")):
            with self.subTest(argv=argv):
                self.lines, self.hist = [], {}
                fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")], self.hist)
                self.resumable("TASK-1", "a", 60)
                self.tui_tick(fake, *argv)
                (launch,) = self.sh.launches()
                self.assertEqual((launch.mode, launch.runner, launch.split, launch.split_from), ("resume", "tui", "below", None))
                self.assertEqual(self.last_run(), ("resume", "TASK-1", "a", "researcher"))

    def test_no_place_for_the_pane_or_a_bad_events_file_exits_2_before_any_linear_call(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        before = self.unmap("\n".join(self.lines) + "\n")
        bad = os.path.join(self.tmp.name, "a#b")
        cases = [(("--tui", "--split-from", "gone"), "no tmux session gone"),
                 (("--now", "--tui", "--dry-run", "--split-from", "gone"), "no tmux session gone"),
                 (("--issue", "TASK-1", "--tui", "--split-from", "gone"), "no tmux session gone"),
                 (("--issue", "TASK-1", "--tui", "--dry-run", "--split-from", "gone"), "no tmux session gone"),
                 (("--tui", "--events", self.tmp.name), f"events file {self.tmp.name}: Is a directory"),
                 (("--issue", "TASK-1", "--tui", "--events", bad), f"events file {bad}: tmux would misread it")]
        for argv, msg in cases:
            with self.subTest(argv=argv):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                layout = attended.Bad(msg) if "gone" in argv else None
                self.assertEqual(self.tui_tick(fake, *argv, layout=layout), 2)
                self.assertEqual(self.err, f"router.py: {msg}\n")
                self.assertEqual((fake.queries, self.sh.calls, self.state), ([], [], before))

    def test_without_tui_no_layout_no_events_and_a_headless_launch(self):
        for tmux in (False, True):
            for argv in ((), ("--now",), ("--issue", "TASK-1"), ("--now", "--dry-run")):
                with self.subTest(tmux=tmux, argv=argv):
                    self.fresh(tmux)
                    fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                    self.assertEqual(self.tui_tick(fake, *argv, layout=AssertionError("layout ran")), 0)
                    want = [] if "--dry-run" in argv else [("new", "headless", None, None, None)]
                    self.assertEqual([(a.mode, a.runner, a.split, a.split_from, a.events) for a in self.sh.launches()], want)
                    self.assertNotIn(DISPLAY, self.sh.calls)
                    self.assertEqual((HINT in self.err, os.listdir(self.agent_pm)), (False, []))

    def test_the_events_file_is_events_else_the_manager_else_the_own_tmux_sessions(self):
        events = os.path.join(self.tmp.name, "events.log")
        rows = ((True, ("--now", "--tui", "--events", events), events, None),
                (True, ("--now", "--tui", "--manager", "m1", f"--events={events}"), events, "m1"),
                (True, ("--now", "--tui"), "mgr", None),
                (True, ("--issue", "TASK-1", "--tui"), "mgr", None),
                (False, ("--now", "--tui", "--manager", "m1"), "m1", "m1"),
                (False, ("--tui", "--manager=m-2", "--now"), "m-2", "m-2"),
                (True, ("--now", "--tui", "--manager", "m1"), "m1", "m1"),
                (True, ("--tui", "--manager=m-2", "--now"), "m-2", "m-2"))
        for tmux, argv, name, manager_ in rows:
            with self.subTest(tmux=tmux, argv=argv):
                self.fresh(tmux)
                want = name if name == events else self.events_of(name)
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tui_tick(fake, *argv), 0)
                (launch,) = self.sh.launches()
                self.assertEqual(launch, outer_args("TASK-1", IDS[DR], ROLE["researcher"], launch.sid, None, "new",
                                                    "tui", None, None, want, manager_))
                self.assertEqual(self.err, "")
                if name == "mgr":   # no --manager or --events: the own tmux session names it
                    self.assertEqual(self.sh.calls[0], DISPLAY)
                else:
                    self.assertNotIn(DISPLAY, self.sh.calls)
                if name == events:
                    self.assertEqual(os.listdir(self.agent_pm), [])
                else:
                    self.assertEqual((self.mode(want), self.mode(os.path.dirname(want))), (0o600, 0o700))

    def test_outside_tmux_without_events_or_manager_the_run_goes_on_without_events_and_says_so(self):
        for argv in (("--now", "--tui"), ("--issue", "TASK-1", "--tui"), ("--tui", "--dry-run", "--now")):
            with self.subTest(argv=argv):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tui_tick(fake, *argv), 0)
                self.assertEqual([l.events for l in self.sh.launches()], [] if "--dry-run" in argv else [None])
                self.assertEqual(self.err.splitlines().count(HINT), 1)
                self.assertTrue(self.err.startswith(HINT + "\n"))
                self.assertEqual(os.listdir(self.agent_pm), [])

    def test_dry_run_resolves_but_creates_or_launches_nothing(self):
        plan = ["plan mode=new queue=1", "pick TASK-1 queue=1"]
        rows = ((False, ("--tui", "--dry-run", "--now"), plan, True),
                (False, ("--tui", "--dry-run", "--issue", "TASK-1"), plan[1:], True),
                (True, ("--tui", "--dry-run", "--now"), plan, False),
                (False, ("--tui", "--dry-run", "--now", "--manager", "m1"), plan, False))
        for tmux, argv, said, hint in rows:
            with self.subTest(tmux=tmux, argv=argv):
                self.fresh(tmux)
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tui_tick(fake, *argv), 0)
                self.assertEqual(self.said()[:len(said)], said)
                self.assertEqual((self.sh.launches(), fake.mutations, self.state), ([], [], ""))
                self.assertEqual(self.layout, [mock.call(None, None)])
                self.assertEqual((HINT in self.err, DISPLAY in self.sh.calls, os.listdir(self.agent_pm)), (hint, tmux, []))

    def test_a_manager_that_fails_exits_2_before_any_linear_call(self):
        elsewhere = os.path.join(self.agent_pm, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, self.managers)
        want = "[A-Za-z0-9_-]+"
        rows = [("%3", "mgr", ("--manager", "a/b"), f"manager 'a/b': want {want}"),
                ("%3", "mgr", ("--manager=",), f"manager '': want {want}"),
                ("%3", "a b", (), f"own tmux session 'a b': want {want}; give --manager <name>"),
                ("x", "mgr", (), "bad $TMUX_PANE 'x'; give --manager <name>"),
                ("%3", "mgr", (), f"manager directory {self.managers}: not a directory owned by you"),
                ("%3", "mgr", ("--manager", "m1"), f"manager directory {self.managers}: not a directory owned by you")]
        for pane, own, extra, msg in rows:
            for argv in (("--tui", "--now", *extra), ("--issue", "TASK-1", "--tui", *extra)):
                with self.subTest(pane=pane, own=own, argv=argv):
                    self.in_tmux(pane)
                    fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                    self.assertEqual(self.tui_tick(fake, *argv, shell=FakeShell(own=own)), 2)
                    self.assertEqual(self.err, f"router.py: {msg}\n")
                    self.assertEqual((fake.queries, self.sh.launches(), self.state), ([], [], ""))
        self.assertEqual(os.listdir(elsewhere), [])

    def test_a_bad_default_events_file_exits_2_before_any_linear_call(self):
        events = self.events_of("mgr")
        os.makedirs(os.path.dirname(events), mode=0o700)
        os.symlink(os.path.join(self.agent_pm, "elsewhere"), events)
        self.in_tmux()
        for argv in (("--tui", "--now"), ("--issue", "TASK-1", "--tui")):
            with self.subTest(argv=argv):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tui_tick(fake, *argv), 2)
                self.assertTrue(self.err.startswith(f"router.py: events file {events}: "), self.err)
                self.assertEqual((fake.queries, self.sh.launches(), self.state), ([], [], ""))


class TaskLabels(Base):
    """The repo's core config, whose researcher ships light-research; deliberately depends on it."""
    ORPHAN = ('Task label "Orphan" is not one of researcher\'s tasks (deep-research, light-research). '
              "Fix the label or the assignee, then move the issue back to Todo.")

    def setUp(self):
        super().setUp()
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG + TASK_LABELS)

    def test_task_for(self):
        outside = [label("Urgent", STRAY, None), label("Light Research", LIGHT, "00000000-0000-4000-8000-000000000003")]
        role_tasks = ["deep-research", "light-research"]
        label_tasks = {LIGHT: "light-research", DEEP: "deep-research", ORPHAN_LABEL: "product-design"}
        fix = "Fix the label or the assignee, then move the issue back to Todo."
        absent = "is not in ~/.agent-pm/orchestrator.local.toml's [task_labels]. Fix the label, then move the issue back to Todo."
        cases = (([], (None, None)),
                 (outside, (None, None)),
                 ([label("Light Research", LIGHT)], ("light-research", None)),
                 ([label("Renamed in Linear", LIGHT)], ("light-research", None)),
                 (outside + [label("Light Research", LIGHT)], ("light-research", None)),
                 ([label("Whatever", DEEP)], ("deep-research", None)),
                 ([label("Light Research", STRAY)], (None, f'Task label "Light Research" {absent}')),
                 ([label("light-research", STRAY)], (None, f'Task label "light-research" {absent}')),
                 ([label("Orphan", ORPHAN_LABEL)], (None, f'Task label "Orphan" is not one of researcher\'s tasks (deep-research, light-research). {fix}')),
                 ([label("Light Research", LIGHT), outside[0], label("Deep Research", DEEP)],
                  (None, "Several task labels (Light Research, Deep Research); keep one, then move the issue back to Todo.")))
        for labels, want in cases:
            self.assertEqual(router.task_for(labels, TASK_GROUP, "researcher", role_tasks, label_tasks), want, labels)

    def test_fr7_invalid_label_goes_to_review(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1, labels=[label("Orphan", ORPHAN_LABEL)]),
                           issue("TASK-2", "Todo", "researcher", priority=2)])
        self.tick(fake)
        t = fake.issues["TASK-1"]
        self.assertEqual((t["state"], t["assignee"], t["comments"], t["subscribers"]),
                         ("In Review", who("researcher"), [self.ORPHAN], ["me@x.com"]))
        self.assertEqual(writes(fake), [("issueSubscribe", "TASK-1"), ("read", "TASK-1"), ("issueUpdate", "TASK-1"),
                                        ("commentCreate", "TASK-1"), ("issueUpdate", "TASK-2")])
        self.assertEqual(fake.mutations[1][1]["s"], STATES["In Review"])
        self.assertIn("claim-skip TASK-1 to=in_review reason=bad task label", self.said())
        self.assertEqual(self.launched(), "TASK-2")
        self.assertEqual(len(self.state.splitlines()), 1)
        self.assertEqual(self.last_run(), ("start", "TASK-2", self.sh.launches()[0].sid, "researcher"))

    def test_capped_and_bad_label_todo_skipped_for_the_next(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1),
                           issue("TASK-2", "Todo", "researcher", priority=2, labels=[label("Orphan", ORPHAN_LABEL)]),
                           issue("TASK-3", "Todo", "pm", priority=3, project=PD),
                           issue("TASK-4", "Todo", "engineer", priority=4, project=PD)])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.tick(fake)
        self.assertEqual(self.said(), ["plan mode=new queue=4", "claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                                       "pick TASK-2 queue=4", "claim-skip TASK-2 to=in_review reason=bad task label",
                                       "pick TASK-3 queue=4", "claim TASK-3 role=pm", "launch TASK-3 exit=0"])
        self.assertEqual({i: fake.issues[i].get("comments") for i in ("TASK-1", "TASK-2")},
                         {"TASK-1": [router.CAP_COMMENT], "TASK-2": [self.ORPHAN]})
        self.assertEqual([i for op, i in writes(fake) if op == "read"], ["TASK-1", "TASK-2"])
        self.assertEqual([v["i"] for q, v in fake.queries if q == RECHECK], ["TASK-2", "TASK-3"])
        self.assertEqual(fake.issues["TASK-4"]["state"], "Todo")

    def test_blocked_invalid_label_waits_for_its_blocker(self):
        for argv, plan, end, rc in (((), ["plan mode=new queue=1"], ["skip reason=nothing claimed"], 0),
                                    (("--issue", "TASK-1"), [], [], 1)):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", labels=[label("Orphan", ORPHAN_LABEL)], inverse=[blocker("TASK-7")])])
            self.assertEqual(self.tick(fake, *argv), rc, argv)
            self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7"]'], argv)
            self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations, self.sh.launches()), ("Todo", [], []), argv)
            self.assertNotIn(RECHECK, [q for q, _ in fake.queries], argv)
            fake.issues["TASK-1"]["inverseRelations"]["nodes"][0]["issue"]["state"]["type"] = "completed"
            self.assertEqual(self.tick(fake, *argv), rc, argv)
            t = fake.issues["TASK-1"]
            self.assertEqual((t["state"], t["comments"]), ("In Review", [self.ORPHAN]), argv)
            self.assertEqual(self.said(), plan + ["pick TASK-1 queue=1", "claim-skip TASK-1 to=in_review reason=bad task label",
                                                  "pick-none reason=nothing claimable"] + end, argv)
            self.assertEqual((self.sh.launches(), self.state), ([], ""), argv)

    def test_fr4_label_read_from_recheck(self):
        class Relabel(FakeLinear):
            def __call__(self, query, **v):
                out = super().__call__(query, **v)
                if "issues(filter" in query:
                    self.issues["TASK-1"]["labels"] = [label("Light Research", LIGHT)]
                return out

        fake = Relabel([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake)
        self.assertIn("claim TASK-1 role=researcher task=light-research", self.said())
        self.assertEqual((self.sh.launches()[0].task, self.sh.launches()[0].mode), ("light-research", "new"))
        self.assertNotIn("labels", fake.reads()[0])

    def test_fr8_dry_run_resolves_nothing(self):
        for argv in (False, True):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", labels=[label("Orphan", ORPHAN_LABEL)])])
            self.assertEqual(self.claim(fake, dry=True, recover=argv), "TASK-1 https://linear.app/x/TASK-1 Deep Research", argv)
            self.assertEqual(self.said(), ["pick TASK-1 queue=1"], argv)
            self.assertEqual(([q for q, _ in fake.queries if q == RECHECK], fake.mutations), ([], []), argv)

    def test_nfr1_claim_reads_one_query_per_issue(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", labels=[label("Light Research", LIGHT)])])
        self.tick(fake)
        self.assertEqual([(q, v) for q, v in fake.queries if "issue(id:" in q], [(RECHECK, {"i": "TASK-1"})])
        todo = [q for q, _ in fake.queries].index(fake.reads()[0])
        self.assertEqual([q for q, _ in fake.queries[todo + 1:]], [RECHECK, fake.mutations[0][0]])

    def test_fr1_tick_checks_the_task_group_once_and_stops_on_a_bad_one(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake)
        self.assertEqual([v for q, v in fake.queries if q == linear.Q_TASK_GROUP], [{"i": TASK_GROUP}])
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        fake.group = {"isGroup": False}
        with self.assertRaises(SystemExit) as cm:
            self.tick(fake)
        self.assertEqual(cm.exception.code, f"orchestrator/config.toml: task_label_group {TASK_GROUP} is not a label group")
        self.assertEqual((fake.queries, fake.mutations), ([(linear.Q_TEAM, {"t": TEAM}), (linear.Q_TASK_GROUP, {"i": TASK_GROUP})], []))
        self.assertEqual(self.sh.calls, [LIST])

    def test_fr20_resume_passes_no_task(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", labels=[label("Light Research", LIGHT)])], self.hist)
        self.moved("TASK-1", 61)
        self.add("resume", "TASK-1", "a", 60)
        self.touch("TASK-1", "a", 40)
        self.tick(fake)
        (launch,) = self.sh.launches()
        self.assertEqual((launch.sid, launch.task, launch.mode), (self.sid("a"), None, "resume"))
        self.assertEqual(self.last_run(), ("resume", "TASK-1", "a", "researcher"))
        self.assertIn("plan TASK-1 mode=resume sid=a", self.said())

    def test_fr22_a_logged_role_not_the_assignees_goes_to_review(self):
        role = "engineer"
        fake = FakeLinear([issue("TASK-1", "In Progress", role, project=PD)], self.hist)
        self.resumable("TASK-1", "a", 60)
        before = self.unmap("\n".join(self.lines) + "\n")
        self.tick(fake)
        t = fake.issues["TASK-1"]
        self.assertEqual((t["state"], t["assignee"], t["comments"], t["subscribers"]),
                         ("In Review", who(role), ["The interrupted agent run's role (researcher) is not the "
                                                   "assignee's (engineer); needs a look."], ["me@x.com"]))
        self.assertEqual(writes(fake), [("issueSubscribe", "TASK-1"), ("read", "TASK-1"), ("issueUpdate", "TASK-1"),
                                        ("commentCreate", "TASK-1")])
        self.assertEqual(fake.mutations[1][1]["s"], STATES["In Review"])
        self.assertEqual(self.said(), ["recover TASK-1 to=in_review reason=role (researcher) is not the assignee's (engineer)"])
        self.assertEqual((self.sh.launches(), self.state), ([], before))

    def test_fr22_every_resumable_issue_is_checked(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", priority=1),
                           issue("TASK-2", "In Progress", "researcher", priority=2)], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.resumable("TASK-2", "b", 60, "pm")
        self.assertEqual(self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-2"]["state"]), ("In Progress", "In Review"))
        self.assertIn("recover TASK-2 to=in_review reason=role (pm) is not the assignee's (researcher)", self.said())

    def test_fr16_requeued_issue_resolved_again_at_claim(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", labels=[label("Light Research", LIGHT)])], self.hist)
        self.moved("TASK-1", 61)
        self.add("start", "TASK-1", "a", 60)
        self.tick(fake)
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.INTERRUPTED])
        self.assertIn("claim TASK-1 role=researcher task=light-research", self.said())
        (launch,) = self.sh.launches()
        self.assertEqual((launch.task, launch.mode), ("light-research", "new"))
        self.assertEqual(self.last_run(), ("start", "TASK-1", launch.sid, "researcher"))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")


TUI_NAME = "engineer-TASK-7-0b6f2c1e"
ATTACH = ("router.py: driver: tmux attach -t '=agent-pm-engineer-TASK-7'\n"
          f"router.py: tui: tmux attach -t '={TUI_NAME}'\n")
# $TUI_ATTACH_PREFIX unset, blank and set, with the prefix each puts before a printed `tmux attach`
PREFIXES = ((None, ""), (" \t", ""), (" docker exec -it box ", "docker exec -it box "))
LAYOUT = attended.layout


class OuterBase(RunBase):
    """The outer on run_fixtures' root, fake Linear and fake gh/git; its stderr lands in self.err."""
    def outer(self, assignee=ENGINEER, task=None, mode="new", runner="headless", ident=ID, sid=SID, **tui):
        a = outer_args(ident, PROJECT, assignee, sid, task, mode, runner, **tui)
        err = io.StringIO()
        with redirect_stderr(err):
            rc = router.outer(a, sh=self.sh, gql=self.gql, run=self.run, projects=self.projects, keychain=self.keychain,
                              root=self.root)
        self.err = err.getvalue()
        return rc

    def handover(self, role="engineer", iterm=""):
        """The inner's argv from the last tmux call, drive.detach's: its handover file, cwd the run dir."""
        argv, kw = self.sh_calls[-1]
        self.assertEqual((argv, kw), (["tmux", "new-session", "-d", "-e", f"ITERM_SESSION_ID={iterm}", "-s",
                                       f"agent-pm-{role}-TASK-7", sys.executable, "-I", "-c", drive.tui_claude.EXEC,
                                       argv[-1]], {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}))
        h = self.handovers[argv[-1]]
        self.assertEqual((h["cwd"], h["env"]["PATH"]), (self.rd, config.PATH))
        return h["argv"]

    def input(self, **kw):
        """The input text the inner gets."""
        *_, last = self.handover(**kw)
        self.assertTrue(last.startswith("--input="), last[:40])
        return last.removeprefix("--input=")


def inner(*args, text=INPUT):
    return [sys.executable, router.RUN, "--uuid", UUID, *args, f"--input={text}"]


class Outer(OuterBase):
    def test_build_starts_the_inner_in_tmux(self):
        self.assertEqual(self.outer(), 0)
        self.assertEqual(len(self.sh_calls), 1)
        self.assertEqual(self.handover(), inner("--target", "Ophis/Agent-PM", *forwarded()))
        self.assertEqual(os.listdir(self.rd), [])
        self.assertEqual(self.gql.calls, [("issue", None, {"i": ID})])
        self.assertEqual(self.run.calls, [(("gh", "api", "repos/ophis/agent-pm"), 60), (LS_REMOTE, 60)])
        self.assertEqual(os.environ["PATH"], config.PATH)
        self.assertEqual((self.err, self.said()), ("", []))

    def test_a_given_task_reaches_the_inner(self):
        self.assertEqual(self.outer(task="light-build"), 0)
        self.assertEqual(self.handover(), inner("--target", "Ophis/Agent-PM", *forwarded(task="light-build")))

    def test_a_long_input_reaches_the_inner_through_the_handover_never_tmux(self):
        big = "".join(f"Requirement {i}: keep the session registry small.\n" for i in range(500)).strip()
        self.gql.issue = node(description=big, comments=[USER_NOTE])
        self.assertEqual(self.outer(), 0)
        text = INPUT.replace("Add a session registry.", big)
        self.assertGreater(len(text.encode()), 24_000)
        self.assertEqual(self.handover(), inner("--target", "Ophis/Agent-PM", *forwarded(), text=text))
        self.assertFalse(any(big in arg for arg in self.sh_calls[-1][0]))
        self.assertEqual(os.listdir(self.rd), [])

    def test_nul_is_removed_and_a_lone_surrogate_replaced_in_the_input(self):
        for description, want in (("Add a session\0 registry.\0", INPUT),
                                  ("Add a session\ud800 registry.", INPUT.replace("Add a session registry.",
                                                                                   "Add a session? registry."))):
            with self.subTest(description=description):
                self.gql.issue = node(description=description, comments=[USER_NOTE])
                self.assertEqual(self.outer(), 0)
                self.assertEqual(self.input(), want)

    def test_research_and_design_have_no_target(self):
        rows = ((RESEARCHER, "researcher", LISTING, "research", "Question", "Compare queues", "Which queue fits?"),
                (PM, "pm", DESIGN_LISTING, "design", "Brief", "Queues PRD", "Build a PRD for X."))
        for who_, role, listing, kind, heading, title, description in rows:
            with self.subTest(role=role):
                self.gql.issue = node(title=title, description=description)
                self.run.table, self.run.calls = [(listing, res("[]"))], []
                self.assertEqual(self.outer(who_), 0)
                self.assertEqual(self.handover(role), inner(*forwarded(who_), text=(
                    f"Reference: TASK-7\nRepo: ophis/agent-pm\nCheckout: TASK-7\n\n{inputs.PRECEDENCE[kind]}\n\n"
                    f"## {heading}\n\n{title}\n\n{description}")))
                self.assertEqual(self.run.calls, [(listing, 60)])

    def local_clone(self):
        """A real clone (no network) of the target in the temp root, configured under [local_clones]; its realpath."""
        path = os.path.realpath(os.path.join(self.tmp, "clone"))
        for argv in (["init", "-q", path], ["-C", path, "remote", "add", "origin", "https://github.com/ophis/agent-pm.git"]):
            subprocess.run(["git", *argv], check=True)
        self.write(self.config, RUN_CONFIG + f'[local_clones]\n"Ophis/Agent-PM" = "{path}"\n')
        return path

    def test_a_configured_local_clone_is_the_inputs_repo(self):
        clone = self.local_clone()
        self.assertEqual(self.outer(), 0)
        self.assertEqual(self.handover(), inner("--target", "Ophis/Agent-PM", *forwarded(),
                                                text=INPUT.replace("Repo: Ophis/Agent-PM", f"Repo: {clone}")))
        self.assertEqual(self.run.calls, [(("gh", "api", "repos/ophis/agent-pm"), 60), (LS_REMOTE, 60)])
        self.assertEqual(self.err, "")

    def test_research_and_design_name_the_local_clone(self):
        clone = self.local_clone()
        for who_, role, listing in ((RESEARCHER, "researcher", LISTING), (PM, "pm", DESIGN_LISTING)):
            with self.subTest(who=who_):
                self.run.table = [(listing, res("[]"))]
                self.assertEqual(self.outer(who_), 0)
                argv = self.handover(role)
                self.assertNotIn("--target", argv)
                self.assertEqual(argv[-1].splitlines()[1], f"Repo: {clone}")

    def test_a_resume_over_a_clone_checkout_keeps_its_repo(self):
        self.local_clone()
        self.transcript()
        name = re.search(r"^Checkout: (.+)$", INPUT, re.M).group(1)
        # Where `repo.py worktree --dir <Workdir>/src --name <Checkout>` puts it, as core's tasks give the command.
        os.makedirs(os.path.join(repo.checkout(os.path.join(self.rd, "src"), "Ophis", "Agent-PM", name), ".git"))
        self.run.table = [(("git", *target.GUARD, "-C"), res()), *ENG_RUN]
        self.assertEqual(self.outer(mode="resume"), 0)
        self.assertEqual(self.input(), INPUT)
        self.assertEqual([argv[0] for argv, _ in self.run.calls], ["gh", "git", "git"])

    def test_resume_rebuilds_the_input(self):
        self.transcript()
        self.assertEqual(self.outer(mode="resume"), 0)
        self.assertEqual(self.handover(), inner("--target", "Ophis/Agent-PM", *forwarded(mode="resume")))

    def test_bad_issue_or_session_id(self):
        for ident, sid in (("task-7", SID), (ID, "not-a-sid"), (ID, "z" * 36)):
            with self.subTest(ident=ident, sid=sid):
                self.assertEqual(self.outer(ident=ident, sid=sid), 2)
                self.assertEqual(self.err, f"router.py: bad issue or session id: {ident} {sid}\n")
        self.assertEqual((self.sh_calls, self.gql.calls), ([], []))

    def test_setup_errors_are_config_errors_of_the_issue(self):
        cases = [(1, {}, "orchestrator/config.toml: unknown keys: bogus"),
                 (2, {"assignee": "x@y.com"}, "'x@y.com' is not a role account"),
                 (2, {"assignee": RESEARCHER, "task": "build"},
                  "task 'build' is not one of researcher's tasks (deep-research, light-research)")]
        for rc, kw, reason in cases:
            with self.subTest(reason=reason):
                self.write(self.config, RUN_CONFIG.replace("human_members", "bogus = 1\nhuman_members") if rc == 1 else RUN_CONFIG)
                self.assertEqual(self.outer(**kw), rc)
                self.assertEqual((self.said()[-1], self.err), (f"router config-error TASK-7 reason={reason}", ""))
        self.assertEqual((self.sh_calls, self.gql.calls), ([], []))

    def test_a_config_load_error_is_one_line(self):
        with mock.patch.object(router, "load_config", side_effect=SystemExit("orchestrator/config.toml:\n  bad")):
            self.assertEqual(self.outer(), 1)
        self.assertEqual(self.said(), ["router config-error TASK-7 reason=orchestrator/config.toml: bad"])

    def test_resume_without_transcript(self):
        self.assertEqual(self.outer(mode="resume"), 3)
        path = config.transcript(ID, SID, self.projects)
        self.assertEqual((self.said(), self.err), ([f"router transient TASK-7 reason=no transcript to resume at {path}"], ""))

    def test_no_keychain_item(self):
        self.missing.add(KEY)
        self.assertEqual(self.outer(), 2)
        self.assertEqual(self.said(), ["router config-error TASK-7 reason=no Keychain item for role key linear-api-key-engineer"])
        self.assertEqual(self.gql.calls, [])

    def test_linear_errors_are_transient(self):
        cases = [(Gql(None), "LookupError: TASK-7: issue not found"),
                 (Gql(node(), lambda n, v: SystemExit("linear api error: boom")), "SystemExit: linear api error: boom")]
        for gql, reason in cases:
            with self.subTest(reason=reason):
                self.gql = gql
                self.assertEqual(self.outer(), 3)
                self.assertEqual(self.said()[-1], f"router transient TASK-7 reason=Linear: {reason}")
        self.assertEqual(self.sh_calls, [])

    def test_invalid_new_bounces_without_a_run(self):
        self.run.table = [(("gh", "api"), res(code=1, stderr="gh: Not Found (HTTP 404)"))]
        reason = "project mapping ophis/agent-pm: not found or no access (HTTP 404)"
        for task in (None, "light-build"):
            with self.subTest(task=task):
                self.gql = Gql(node(comments=[USER_NOTE]))
                self.assertEqual(self.outer(task=task), 0)
                self.assertEqual(self.gql.calls, [
                    ("issue", None, {"i": ID}),
                    ("subscribe", KEY, {"i": UUID, "e": "me@x.com"}),
                    ("comment", KEY, {"i": UUID, "b": f"Question: repo check failed: {reason}. Fix the description's "
                                                      "`Repo:` line, then move this issue back to Todo."}),
                    ("reread", KEY, {"i": UUID}),
                    ("state", KEY, {"i": UUID, "s": IDS_BY_KEY["in_review"]})])
                self.assertEqual(self.said()[-1], f"router bounce TASK-7 reason={reason}")
                self.assertEqual((self.sh_calls, os.path.exists(self.rd)), ([], False))

    def test_bounce_failure_is_transient(self):
        self.run.table = [(("gh", "api"), res(code=1, stderr="HTTP 404"))]
        self.gql = Gql(node(), lambda n, v: SystemExit("linear api error: boom") if n == "comment" else None)
        self.assertEqual(self.outer(), 3)
        self.assertEqual(self.said(), ["router transient TASK-7 reason=bounce: SystemExit: linear api error: boom"])
        self.assertEqual(self.sh_calls, [])

    def test_invalid_resume_and_transient_check_start_nothing(self):
        self.transcript()
        cases = [("resume", res(code=1, stderr="HTTP 404"),
                  "project mapping ophis/agent-pm: not found or no access (HTTP 404)"),
                 ("new", res(code=1, stderr="HTTP 502: Bad Gateway"), "gh api repos/ophis/agent-pm: HTTP 502: Bad Gateway"),
                 ("new", subprocess.TimeoutExpired(["gh", "api"], 60),
                  "repo check: TimeoutExpired: Command '['gh', 'api']' timed out after 60 seconds")]
        for mode, r, reason in cases:
            with self.subTest(reason=reason):
                self.run.table = [(("gh", "api"), r)]
                self.assertEqual(self.outer(mode=mode), 3)
                self.assertEqual(self.said()[-1], f"router transient TASK-7 reason={reason}")
        self.assertEqual(self.gql.calls, [("issue", None, {"i": ID})] * 3)
        self.assertEqual(self.sh_calls, [])

    def test_docs_failure_is_transient(self):
        self.gql.issue = node(title="Compare queues", description="Which queue fits?")
        for r, reason in ((res(code=1, stderr="HTTP 500"), "RuntimeError: gh api contents Research: HTTP 500"),
                          (OSError("gh missing"), "OSError: gh missing")):
            with self.subTest(reason=reason):
                self.run.table = [(LISTING, r)]
                self.assertEqual(self.outer(RESEARCHER), 3)
                self.assertEqual(self.said()[-1], f"router transient TASK-7 reason=docs: {reason}")
        self.assertEqual(self.sh_calls, [])

    def test_a_run_dir_that_cannot_be_made_starts_nothing(self):
        self.write(self.rd, "")
        self.assertEqual(self.outer(), 1)
        self.assertEqual(self.sh_calls, [])
        self.assertEqual((self.said(), self.err),
                         ([f"router launch-error TASK-7 error=input: FileExistsError: [Errno 17] File exists: '{self.rd}'"], ""))

    def test_a_driver_session_that_cannot_start_exits_1(self):
        self.tmux_error = "duplicate session: agent-pm-engineer-TASK-7\n"
        self.assertEqual(self.outer(), 1)
        self.assertEqual(len(self.sh_calls), 1)
        self.assertEqual(self.handovers, {})
        self.assertFalse(os.path.exists(self.sh_calls[0][0][-1]))
        self.assertEqual(self.err, "")
        self.assertEqual(self.said(), ["router launch-error TASK-7 error=RunnerError: tmux: duplicate session: "
                                       "agent-pm-engineer-TASK-7"])


class Attended(OuterBase):
    """The outer with the tui runner; attended.layout sees a fake tmux, no $TMUX or $ITERM_SESSION_ID, and iTerm2's
    $TERM_PROGRAM."""
    def setUp(self):
        super().setUp()
        for k in ("TMUX", "ITERM_SESSION_ID"):  # RunBase's patch.dict restores them
            os.environ.pop(k, None)
        os.environ["TERM_PROGRAM"] = "iTerm.app"

    def tui(self, live=(), assignee=ENGINEER, **opts):
        self.tmux = Tmux(live)
        place = functools.partial(LAYOUT, proc=self.tmux)
        with mock.patch.object(attended, "layout", place):
            return self.outer(assignee, runner="tui", **opts)

    def driver(self, *tail, iterm=""):
        """The one tmux call; the inner's argv."""
        self.assertEqual(len(self.sh_calls), 1)
        self.assertEqual(self.handover(iterm=iterm), inner("--target", "Ophis/Agent-PM", *forwarded(), *tail))

    def rename_engineer(self, name):
        """The root's core (linked to the real one) and orchestrator config with the engineer role named `name`."""
        core, roles = os.path.join(self.root, "core"), os.path.join("team", "roles")
        os.remove(core)
        for parent, own in (("", {compose.CONFIG, "team"}), ("team", {"roles"}), (roles, set())):
            os.makedirs(os.path.join(core, parent), exist_ok=True)
            for n in set(os.listdir(os.path.join(config.CORE, parent))) - own:
                os.symlink(os.path.join(config.CORE, parent, n), os.path.join(core, parent, n))
        os.symlink(os.path.join(config.CORE, roles, "engineer.md"), os.path.join(core, roles, f"{name}.md"))
        self.write(os.path.join(core, compose.CONFIG),
                   self.read(os.path.join(config.CORE, compose.CONFIG)).replace("[roles.engineer", f"[roles.{name}"))
        self.write(os.path.join(hermetic.home(self), "core.local.toml"), self.read(hermetic.FIXTURE))
        self.write(self.config, RUN_CONFIG.replace(role_table("engineer"), role_table(name)))
        p = mock.patch.dict(config.TASKS, {name: config.TASKS["engineer"]})
        p.start()
        self.addCleanup(p.stop)

    def test_the_pane_place_reaches_the_inner(self):
        """split from a session; an iTerm2 pane gets the TUI and a detached driver; the caller's tmux session is the
        opener, never split-from."""
        rows = (("split-from", {}, dict(split="below", split_from="dev", live=["dev"]),
                 ["--split=below", "--split-from=dev"], ""),
                ("iterm2", {"ITERM_SESSION_ID": "w0t0p0:ABC"}, {}, ["--opener=w0t0p0:ABC"], "w0t0p0:ABC"),
                ("tmux", {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%3"}, dict(live=["mine"]), ["--opener=mine"], ""))
        for name, env, opts, tail, iterm in rows:
            with self.subTest(name), mock.patch.dict(os.environ, env):
                self.sh_calls, self.options = [], []
                self.assertEqual(self.tui(**opts), 0)
                self.driver("--runner=tui", *tail, iterm=iterm)
                self.assertEqual(self.err, ATTACH)
                opener = [a[9:] for a in tail if a.startswith("--opener=")]   # the driver session carries it too
                self.assertEqual(self.options, [("=agent-pm-engineer-TASK-7:", "@opener", o) for o in opener])

    def test_events_reach_the_inner_as_an_absolute_path(self):
        os.environ["ITERM_SESSION_ID"] = "w0t0p0:ABC"
        events = os.path.join(self.tmp, "events.log")
        self.assertEqual(self.tui(split="below", events=os.path.join(self.tmp, ".", "events.log")), 0)
        self.driver("--runner=tui", "--split=below", "--opener=w0t0p0:ABC", f"--events={events}", iterm="w0t0p0:ABC")
        self.assertEqual(os.stat(events).st_mode & 0o777, 0o600)

    def test_a_role_outside_the_session_name_contract_starts_nothing(self):
        os.environ["ITERM_SESSION_ID"] = "w0t0p0:ABC"
        self.rename_engineer("eng_x")
        self.assertEqual(self.tui(assignee="eng_x@agents.test"), 2)
        (line,) = self.said()
        self.assertTrue(line.startswith("router config-error TASK-7 reason=ValueError: TUI session prefix 'eng_x-TASK-7': want "),
                        line)
        self.assertEqual((self.sh_calls, self.gql.calls, self.keychain_calls, self.run.calls), ([], [], [], []))
        self.assertEqual(self.outer("eng_x@agents.test"), 0)  # headless: no TUI session to name

    def test_attach_lines_come_before_the_driver_session(self):
        printed, sh = [], self.sh
        self.sh = lambda argv, **kw: (argv[1] == "new-session" and printed.append(sys.stderr.getvalue())
                                      or sh(argv, **kw))
        os.environ["ITERM_SESSION_ID"] = "w0t0p0:ABC"
        self.assertEqual(self.tui(), 0)
        self.assertEqual(printed, [ATTACH])

    def test_attach_lines_take_the_prefix(self):
        os.environ["ITERM_SESSION_ID"] = "w0t0p0:ABC"
        for value, prefix in PREFIXES:
            with self.subTest(value=value):
                if value is not None:
                    os.environ["TUI_ATTACH_PREFIX"] = value
                self.assertEqual(self.tui(), 0)
                self.assertEqual(self.err, ATTACH.replace("tmux attach", prefix + "tmux attach"))

    def test_no_place_for_the_pane_or_a_bad_events_file_starts_nothing(self):
        iterm = {"ITERM_SESSION_ID": "w0t0p0:ABC"}
        cases = [({}, {}, "no pane to show the TUI beside (no anchor pane: not in tmux, no iTerm2 pane ($ITERM_SESSION_ID)): "
                          "run from tmux or iTerm2, or pass --split-from SESSION"),
                 ({}, {"split": "left"}, "split must be one of right, below"),
                 ({}, {"split_from": "gone"}, "no tmux session gone"),
                 (iterm, {"events": self.tmp}, f"events file {self.tmp}: Is a directory"),
                 (iterm, {"events": os.path.join(self.tmp, "a#b")}, f"events file {self.tmp}/a#b: tmux would misread it")]
        for env, opts, msg in cases:
            with self.subTest(msg=msg), mock.patch.dict(os.environ, env):
                self.assertEqual(self.tui(**opts), 2)
                self.assertEqual(self.err, f"router.py: {msg}\n")
        self.assertEqual((self.sh_calls, self.gql.calls, self.keychain_calls, self.run.calls), ([], [], [], []))
        self.assertFalse(os.path.exists(self.rd))


class ClaimLinear(FakeLinear):
    """FakeLinear for the claim; issues.Q_ISSUE, the outer's read, gets the outer's issue node."""
    def __init__(self, todo, outer_issue):
        super().__init__([todo])
        self.issue = outer_issue

    def __call__(self, query, **v):
        if query == issues.Q_ISSUE:
            self.queries.append((query, v))
            return {"issue": self.issue}
        return super().__call__(query, **v)


class AttendedEntry(OuterBase):
    """router.py --issue ID --tui: the claim on a fake Linear, then the real outer with the tui runner. Outside tmux: the
    stderr hint (no manager directory) is left out of self.err and noted in self.hinted. A HOME of its own."""
    def setUp(self):
        super().setUp()
        self.managers = os.path.join(hermetic.home(self), "managers")
        self.owns = []
        self.todo = issue(ID, "Todo", "engineer")
        self.todo["project"] = {"id": PROJECT, "name": "Agent PM"}
        self.gql = ClaimLinear(self.todo, node(comments=[USER_NOTE]))
        self.tmux = Tmux(live=["dev"])
        for k in ("TMUX", "TMUX_PANE", "ITERM_SESSION_ID"):
            os.environ.pop(k, None)
        os.environ["TERM_PROGRAM"] = "iTerm.app"
        p = mock.patch.object(attended, "layout", functools.partial(LAYOUT, proc=self.tmux))
        p.start()
        self.addCleanup(p.stop)

    def sh(self, argv, **kw):
        """RunBase's, noting runs.jsonl's text when the driver session starts; display-message prints the next of
        owns (the caller's tmux session) while one is left; the usage probe answers five_hour 0.2."""
        if argv[1] == "new-session":
            self.at_launch.append(self.read(self.runs))
        if argv[:2] == ["tmux", "display-message"] and self.owns:
            self.sh_calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, self.owns.pop(0) + "\n", "")
        if argv[0] == "claude":
            self.sh_calls.append((argv, kw))
            ev = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": 0.2}, "seven_day": {"utilization": 0.1}}}}
            return subprocess.CompletedProcess(argv, 0, json.dumps(ev) + "\n", "")
        return super().sh(argv, **kw)

    def main(self, argv, tty=False):
        err = Stderr(tty)
        with redirect_stderr(err):
            rc = router.main(argv, gql=self.gql, tdir=self.projects, config=self.config, runs=self.runs, sh=self.sh,
                             root=self.root, run=self.run, keychain=self.keychain)
        self.hinted = HINT + "\n" in err.getvalue()
        self.err = err.getvalue().replace(HINT + "\n", "")
        return rc

    def entry(self, *extra, tty=False):
        return self.main(["--issue", ID, "--tui", *extra], tty)

    def started(self):
        """The sid of runs.jsonl's one line, the start of ID as engineer; no runs.log beside it."""
        (line,) = self.read(self.runs).splitlines()
        record = json.loads(line)
        datetime.fromisoformat(record.pop("ts"))
        sid = record.pop("sid")
        self.assertRegex(sid, config.UUID_RE)
        self.assertEqual(record, {"kind": "start", "issue": ID, "role": "engineer"})
        self.assertNotIn("runs.log", os.listdir(os.path.dirname(self.runs)))
        return sid

    def test_claims_then_starts_the_run_attended(self):
        events = os.path.join(self.tmp, "events.log")
        cases = [(("--split", "below", "--split-from", "dev"), "", ["--runner=tui", "--split=below", "--split-from=dev"]),
                 ((), "w0t0p0:ABC", ["--runner=tui", "--opener=w0t0p0:ABC"]),
                 (("--events", events), "w0t0p0:ABC", ["--runner=tui", "--opener=w0t0p0:ABC", f"--events={events}"])]
        for extra, iterm, tail in cases:
            with self.subTest(extra=extra):
                os.environ["ITERM_SESSION_ID"] = iterm
                self.todo["state"], self.sh_calls, self.gql.mutations = "Todo", [], []
                self.tmux_sessions = [TUI_NAME, "agent-pm-engineer-TASK-70", "agent-pm-engineer-TASK-8"]
                if os.path.exists(self.runs):
                    os.remove(self.runs)
                self.at_launch = []
                self.assertEqual(self.entry(*extra), 0)
                self.assertEqual(self.hinted, "--events" not in extra)
                sid = self.started()
                self.assertEqual(self.at_launch, [self.read(self.runs)])
                self.assertEqual(self.sh_calls[:-1], [(LIST, {"capture_output": True, "text": True})])
                self.assertEqual(self.handover(iterm=iterm),
                                 inner("--target", "Ophis/Agent-PM", *forwarded(sid=sid), *tail))
                self.assertEqual(self.said("router")[-2:], ["pick TASK-7 queue=1", "claim TASK-7 role=engineer"])
                self.assertEqual(self.err.splitlines(), ["router.py: driver: tmux attach -t '=agent-pm-engineer-TASK-7'",
                                                         f"router.py: tui: tmux attach -t '=engineer-TASK-7-{sid[:8]}'"])
                self.assertEqual(self.gql.mutations, [(linear.M_STATE, {"i": ID, "s": IDS_BY_KEY["in_progress"]})])

    def test_a_task_label_reaches_the_inner_as_task(self):
        self.write(self.config, RUN_CONFIG + f'[task_labels]\nlight-build = "{LIGHT}"\n')
        self.todo["labels"], self.at_launch = [label("Light Build", LIGHT)], []
        self.assertEqual(self.entry("--split-from", "dev"), 0)
        sid = self.started()
        self.assertEqual(self.handover(), inner("--target", "Ophis/Agent-PM", *forwarded(task="light-build", sid=sid),
                                                "--runner=tui", "--split-from=dev"))
        self.assertIn("claim TASK-7 role=engineer task=light-build", self.said("router"))

    def test_only_issue_split_split_from_and_events(self):
        for argv in (["--issue", ID, "--tui", *x] for x in (
                ["--project", PROJECT], ["--assignee", ENGINEER], ["--sid", SID], ["--task", "build"], ["--mode", "new"],
                ["--inner"], ["--uuid", UUID], ["--target", "Ophis/Agent-PM"], ["--runner", "tui"], ["--runner", "headless"],
                ["--opener", "mine"], ["--spl", "below"], ["--bes", "dev"], ["--eve", "x"])):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(argv), 2)
                self.assertEqual(self.err, router.USAGE + "\n")
        self.assertEqual((self.sh_calls, self.gql.queries, self.tmux.calls), ([], [], []))

    def test_config_error_exits_1_logged_once_a_day_and_printed_on_a_terminal(self):
        self.write(self.config, RUN_CONFIG.replace("human_members", "bogus = 1\nhuman_members"))
        for tty, err in ((False, ""), (True, "router.py: orchestrator/config.toml: unknown keys: bogus\n")):
            self.assertEqual(self.entry(tty=tty), 1)
            self.assertEqual(self.err, err)
            self.assertEqual(self.said(), ["router config-error reason=orchestrator/config.toml: unknown keys: bogus"])
        self.assertEqual((self.sh_calls, self.gql.queries, self.tmux.calls), ([], [], []))

    def test_a_config_core_rejects_is_logged_once_a_day_before_a_tick_or_an_issue(self):
        self.write(self.config, RUN_CONFIG + role_table("bogus"))
        reason = "orchestrator/config.toml: role 'bogus' is not in core/config.toml"
        for argv in (["--now"], ["--issue", ID]):
            for tty, err in ((False, ""), (True, f"router.py: {reason}\n")):
                with self.subTest(argv=argv, tty=tty):
                    self.assertEqual(self.main(argv, tty), 1)
                    self.assertEqual(self.err, err)
        self.assertEqual(self.said(), [f"router config-error reason={reason}"])
        self.assertEqual((self.sh_calls, self.gql.queries, self.tmux.calls), ([], [], []))

    def test_no_place_for_the_pane_or_a_bad_events_file_before_any_linear_call(self):
        cases = [((), "no pane to show the TUI beside (no anchor pane: not in tmux, no iTerm2 pane ($ITERM_SESSION_ID)): "
                      "run from tmux or iTerm2, or pass --split-from SESSION"),
                 (("--split", "left"), "split must be one of right, below"),
                 (("--split-from", "gone"), "no tmux session gone"),
                 (("--split-from", "dev", "--events", self.tmp), f"events file {self.tmp}: Is a directory")]
        for extra, msg in cases:
            with self.subTest(msg=msg):
                self.assertEqual(self.entry(*extra), 2)
                self.assertEqual((self.err, self.hinted), (f"router.py: {msg}\n", False))
        self.assertEqual((self.sh_calls, self.gql.queries, os.path.exists(self.runs)), ([], [], False))
        self.assertEqual(os.environ["PATH"], config.PATH)

    def test_a_live_run_is_refused(self):
        for role in ("engineer", "pm"):
            with self.subTest(role=role):
                self.sh_calls, self.tmux_sessions = [], [f"agent-pm-{role}-{ID}"]
                self.assertEqual(self.entry("--split-from", "dev"), 1)
                self.assertEqual(self.err, f"router.py: {ID} has a live agent run: tmux attach -t '=agent-pm-{role}-{ID}'\n")
                self.assertEqual(self.sh_calls, [(LIST, {"capture_output": True, "text": True})])
        self.assertEqual((self.gql.queries, os.path.exists(self.runs)), ([], False))

    def test_the_live_run_refusal_takes_the_prefix(self):
        self.tmux_sessions = [f"agent-pm-engineer-{ID}"]
        for value, prefix in PREFIXES:
            with self.subTest(value=value):
                if value is not None:
                    os.environ["TUI_ATTACH_PREFIX"] = value
                self.assertEqual(self.entry("--split-from", "dev"), 1)
                self.assertEqual(self.err, f"router.py: {ID} has a live agent run: {prefix}tmux attach -t "
                                           f"'=agent-pm-engineer-{ID}'\n")

    def test_another_router_running_leaves_the_issue_alone(self):
        """TASK-237: a tick claimed it, its driver session not up yet; --issue must not send it back to Todo."""
        self.todo["state"] = "In Progress"
        with other_router(self.runs):
            self.assertEqual(self.entry("--split-from", "dev"), 1)
        self.assertEqual(self.err, "router.py: another router is running; try again\n")
        self.assertEqual(self.said(), ["router skip TASK-7 reason=another router is running"])
        self.assertEqual((self.todo["state"], self.gql.queries, self.sh_calls, os.path.exists(self.runs)),
                         ("In Progress", [], [], False))
        self.assertEqual(self.entry("--split-from", "dev"), 1)
        self.assertEqual(self.said("router")[1:], [f"recover TASK-7 to=todo reason=no session updated={self.todo['updatedAt']}"])
        self.assertEqual(self.todo["state"], "Todo")

    def test_nothing_started_exits_1_without_a_start_line(self):
        cases = [(dict(state="In Review"), "pick-none TASK-7 reason=not a Todo issue assigned to a role account", "In Review"),
                 (dict(state="In Progress"), f"recover TASK-7 to=todo reason=no session updated={self.todo['updatedAt']}", "Todo"),
                 (dict(inverseRelations={"nodes": [blocker("TASK-9")]}), 'blocked TASK-7 by=["TASK-9"]', "Todo"),
                 (dict(labels=[label("Stray", STRAY)]), "claim-skip TASK-7 to=in_review reason=bad task label", "In Review")]
        for change, said, state in cases:
            with self.subTest(said=said):
                self.todo.update(state="Todo", inverseRelations={"nodes": []}, labels=[])
                self.todo.update(change)
                self.sh_calls = []
                self.assertEqual(self.entry("--split-from", "dev"), 1)
                self.assertIn(said, self.said("router"))
                self.assertEqual((self.todo["state"], self.sh_calls), (state, [(LIST, {"capture_output": True, "text": True})]))
        self.assertFalse(os.path.exists(self.runs))

    def fresh(self):
        """ID back in Todo, no runs.jsonl, no tmux call yet."""
        self.todo["state"], self.sh_calls, self.gql.mutations, self.at_launch = "Todo", [], [], []
        if os.path.exists(self.runs):
            os.remove(self.runs)

    def detached(self, argv):
        """main(argv), drive.detach called once: (exit code, the roster drive.detach got)."""
        with mock.patch.object(drive, "detach", wraps=drive.detach) as detach:
            rc = self.main(argv)
        (call,) = detach.call_args_list
        return rc, call.kwargs.get("roster")

    def pipeline(self, sid, d, opener, split):
        """drive.detach's roster for ID's run sid, split from dev: d and the pipeline entry's fields."""
        return d, {"kind": "pipeline", "sid": sid, "cwd": self.rd, "note": ID, "tui": f"engineer-{ID}-{sid[:8]}",
                   "opener": opener, "split": split, "split_from": "dev",
                   "resume": [sys.executable, os.path.abspath(router.__file__), "--issue", ID, "--tui", "--events",
                              os.path.join(d, "events"), "--manager", os.path.basename(d)]}

    def attach_lines(self, sid):
        return [f"router.py: driver: tmux attach -t '=agent-pm-engineer-{ID}'",
                f"router.py: tui: tmux attach -t '=engineer-{ID}-{sid[:8]}'"]

    def test_a_manager_directory_gets_the_pipeline_entry_through_drive_detach(self):
        m1, mgr = os.path.join(self.managers, "m1"), os.path.join(self.managers, "mgr")
        iterm = "w0t0p0:ABC"
        cases = [(["--issue", ID, "--tui", "--manager", "m1", "--split", "below", "--split-from", "dev"], False, m1,
                  iterm, "below"),
                 (["--now", "--tui", "--manager=m1", "--split=below", "--split-from=dev"], False, m1, iterm, "below"),
                 (["--issue", ID, "--tui", "--split-from", "dev"], True, mgr, "mine", None)]
        for argv, tmux, d, opener, split in cases:
            with self.subTest(argv=argv):
                self.fresh()
                if tmux:
                    os.environ.update(TMUX="/tmp/tmux-501/default,1,0", TMUX_PANE="%3", ITERM_SESSION_ID="")
                    self.owns = ["mgr", "mgr"]
                else:
                    os.environ["ITERM_SESSION_ID"] = iterm
                rc, roster = self.detached(argv)
                self.assertEqual(rc, 0)
                sid = self.started()
                self.assertEqual(roster, self.pipeline(sid, d, opener, split))
                tail = [*([f"--split={split}"] if split else []), "--split-from=dev", f"--opener={opener}",
                        f"--events={d}/events"]
                self.assertEqual(self.handover(iterm="" if tmux else iterm),
                                 inner("--target", "Ophis/Agent-PM", *forwarded(sid=sid), "--runner=tui", *tail))
                with manager.roster(d, write=False) as r:
                    self.assertEqual(r["entries"], {f"agent-pm-engineer-{ID}": {**roster[1], "pane": None,
                                                                                "state": "working", "started": mock.ANY}})
                self.assertEqual((self.err.splitlines(), self.owns), (self.attach_lines(sid), []))

    def test_without_a_roster_directory_drive_detach_gets_no_roster(self):
        m1 = os.path.join(self.managers, "m1")
        manager.ensure(m1)
        cases = [["--issue", ID, "--tui", "--split-from", "dev", "--manager", "m1", "--events",
                  os.path.join(self.tmp, "events.log")],
                 ["--issue", ID, "--tui", "--split-from", "dev", "--events", os.path.join(m1, "events")],
                 ["--issue", ID, "--tui", "--split-from", "dev"],
                 ["--issue", ID]]
        for argv in cases:
            with self.subTest(argv=argv):
                self.fresh()
                self.assertEqual(self.detached(argv), (0, None))
                self.started()
                self.assertNotIn("roster.json", os.listdir(m1))

    def test_an_entry_write_failure_is_one_line_and_the_run_goes_on(self):
        m1 = os.path.join(self.managers, "m1")
        manager.ensure(m1)
        path = os.path.join(m1, "roster.json")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write("{")
        try:
            json.loads("{")
        except ValueError as e:
            why = f"{path}: invalid JSON: {e}"
        self.at_launch = []
        rc, roster = self.detached(["--issue", ID, "--tui", "--manager", "m1", "--split-from", "dev"])
        self.assertEqual(rc, 0)
        sid = self.started()
        self.assertEqual(roster, self.pipeline(sid, m1, None, None))
        self.assertEqual(self.err.splitlines(),
                         [*self.attach_lines(sid), f"manager: entry agent-pm-engineer-{ID} not written: {why}"])
        self.assertEqual(self.read(path), "{")

    def test_a_roster_directory_that_cannot_be_resolved_is_one_line_and_the_run_goes_on(self):
        os.environ.update(TMUX="/tmp/tmux-501/default,1,0", TMUX_PANE="%3")
        self.owns, self.at_launch = ["mgr", "a b"], []
        self.assertEqual(self.detached(["--issue", ID, "--tui", "--split-from", "dev"]), (0, None))
        sid = self.started()
        self.assertEqual(self.err.splitlines(),
                         [*self.attach_lines(sid), f"manager: entry agent-pm-engineer-{ID} not written: own tmux session "
                                                   "'a b': want [A-Za-z0-9_-]+; give --manager <name>"])
        self.assertNotIn("roster.json", os.listdir(os.path.join(self.managers, "mgr")))


if __name__ == "__main__":
    unittest.main()
