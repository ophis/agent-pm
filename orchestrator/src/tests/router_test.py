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
from board_ids import ACCOUNTS, HEADER, STATES as IDS_BY_KEY, TASK_GROUP, TEAM, role as role_table, team_node  # noqa: E402
import config  # noqa: E402
import attended  # noqa: E402
import linear  # noqa: E402
import router  # noqa: E402
import drive  # noqa: E402

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
            if "id" in f:
                i = self.issues.get(f["id"]["eq"]) if f["team"]["id"]["eq"] == TEAM else None
                return {"issues": {"nodes": [{"assignee": i["assignee"]}] if i else []}}
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
        self.tdir = os.path.join(self.tmp.name, "transcripts")
        os.makedirs(self.tdir)
        self.log = os.path.join(self.tmp.name, "runs.log")
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

    def resumable(self, ident, sid, minutes_ago, task=None):
        """A start line for sid, an old <sid>.jsonl, and a move to In Progress just before the start."""
        self.moved(ident, minutes_ago + 1)
        self.add("start", ident, sid, minutes_ago, task)
        self.touch(ident, sid, 40)

    def add(self, kind, ident, sid, minutes_ago, task=None):
        """A runs.log line; without task it is a line from before task= was recorded."""
        ts = (NOW - timedelta(minutes=minutes_ago)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        s = self.sid(sid)
        self.lines.append(f"{ts} {kind} {ident} session={s}" + (f" task={task}" if task else ""))

    def touch(self, ident, sid, minutes_ago, sub=None):
        """<sid>.jsonl, or sub under <sid>/, in the issue's transcript folder."""
        p = config.transcript(ident, self.sid(sid), self.tdir)
        if sub:
            p = os.path.join(p[:-len(".jsonl")], sub)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
        t = (NOW - timedelta(minutes=minutes_ago)).timestamp()
        os.utime(p, (t, t))

    def board(self, fake, use, dry=False):
        """use(Board) over runs.log lines; the logged messages land in self.err."""
        with open(self.log, "w") as f:
            f.write("\n".join(self.lines) + "\n")
        err = io.StringIO()
        with redirect_stderr(err):
            out = use(router.Board(fake, router.parse_log(self.log), self.tdir, NOW, dry, config.load_config(self.config),
                                   root=self.root))
        self.err = self.unmap(err.getvalue())
        return out

    def plan(self, fake, dry=False):
        """Recover, then "resume <ID> <SID> <url> <project>", "new" or ""."""
        run = self.board(fake, lambda b: b.next_run(b.recover()), dry)
        if run and run[0] == "resume":
            _, issue, sid, _ = run
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
        """The logged messages, without timestamps."""
        return [line.split(" ", 2)[2] for line in self.err.splitlines()]

    def tick(self, fake, *argv, hour=2, shell=None):
        self.sh = shell or FakeShell()
        with open(self.log, "w") as f:
            f.write("\n".join(self.lines) + ("\n" if self.lines else ""))
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}):
            rc = router.main(list(argv), gql=fake, now=NOW, tdir=self.tdir, config=self.config, runs=self.log,
                             sh=self.sh, hour=hour, root=self.root)
            self.path = os.environ["PATH"]
        self.err = self.unmap(err.getvalue())
        with open(self.log) as f:
            self.state = self.unmap(f.read())
        return rc

    def launched(self):
        (launch,) = self.sh.launches()
        return launch[launch.index("--issue") + 1]

    def launched_runs(self):
        """(ID, mode) of each run.py launch, in order."""
        return [(c[c.index("--issue") + 1], c[-1]) for c in self.sh.launches()]

    def probes(self):
        return [c[0] for c in self.sh.calls].count("claude")


class ParseAndLiveness(Base):
    def test_missing_log_is_empty(self):
        self.assertEqual(router.parse_log(os.path.join(self.tmp.name, "nope")), [])

    def test_fr9_parse_task(self):
        self.add("start", "TASK-1", "a", 60, "deep-research")
        self.add("start", "TASK-2", "b", 50)
        self.add("resume", "TASK-1", "a", 40, "light-research")
        self.add("resume", "TASK-1", "a", 30)
        self.lines += ["2026-09-26 23:00:00 resume TASK-3 session=c n=1 task=x extra",
                       "pick: TASK-1 (3 in queue)", "2026-09-26 13:04:05 start --dry-run log=/x.jsonl",
                       "2026-09-26 20:45:59 skip: queue empty", "recover: TASK-2 (last updated x)",
                       "2026-09-26 23:00:00 end TASK-1 session=a exit=1"]
        with open(self.log, "w") as f:
            f.write("\n".join(self.lines) + "\n")
        entries = router.parse_log(self.log)
        self.assertEqual([e[1:] for e in entries], [("start", "TASK-1", self.sid("a"), "deep-research"),
                                                    ("start", "TASK-2", self.sid("b"), None),
                                                    ("resume", "TASK-1", self.sid("a"), "light-research"),
                                                    ("resume", "TASK-1", self.sid("a"), None),
                                                    ("resume", "TASK-3", "c", None)])
        self.assertAlmostEqual(entries[0][0].timestamp(), (NOW - timedelta(minutes=60)).timestamp())
        self.assertEqual([router.logged_task(entries, s) for s in (self.sid("a"), self.sid("b"), "c", "nope")],
                         ["light-research", None, None, None])

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
    def test_no_event(self):
        self.assertEqual(router.gate(['{"type":"system"}', "garbage"]), (False, "no rate_limit_event"))

    def test_ok_from_the_last_event(self):
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

    def test_max_5h(self):
        self.assertEqual([router.gate([event(five=0.85)], *m)[0] for m in ((), (0.8,))], [True, False])


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
    def test_nothing(self):
        fake = FakeLinear([issue("TASK-1", "In Review", "researcher")])
        self.assertEqual(self.plan(fake), "")

    def test_new(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.plan(fake), "new")
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
        self.resumable("TASK-4", "d", 200)
        cands = self.board(fake, lambda b: b.recover({"TASK-1"}))
        self.assertEqual([(i["identifier"], self.unmap(sid), task) for i, sid, task in cands],
                         [("TASK-3", "c", "deep-research"), ("TASK-4", "d", "product-design")])
        self.assertEqual(self.said(), [f"recover: TASK-2 (last updated {ago(hours=3)})"])
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
                for ident, _, m, _, _ in specs:
                    self.resumable(ident, ident.lower(), m)
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
                self.assertIn("recover: TASK-1 session=a has no transcript", self.err)
                self.assertIn("recover: TASK-5 reached 4 attempts", self.err)
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

    def test_user_reset_resumes_new_sid(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.reset_fixture()
        self.assertEqual(self.plan(fake), "resume TASK-1 new https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(fake.mutations, [])

    def test_user_reset_orders_by_new_sid(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=2), issue("TASK-2", "In Progress", "researcher", priority=2))
        self.reset_fixture()
        self.resumable("TASK-2", "b", 200)
        self.assertEqual(self.plan(fake), "resume TASK-2 b https://linear.app/x/TASK-2 Deep Research")

    def test_recover_skips_other_assignees(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "someone", updated=ago(hours=3)),
                           issue("TASK-2", "In Progress", None, updated=ago(hours=3))])
        self.plan(fake)
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

    def test_attempt_cap_reset_by_user(self):
        fake, out = self.capped([{"createdAt": ago(minutes=380), "actorId": USER, "toStateId": STATES["Todo"]}])
        self.assertEqual(out, "resume TASK-1 d https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(fake.mutations, [])

    def test_attempt_cap_reset_only_by_human_members(self):
        fake, out = self.capped([{"createdAt": ago(minutes=380), "actorId": AGENT, "toStateId": STATES["Todo"]},
                                 {"createdAt": ago(minutes=375), "actorId": "role-account", "toStateId": STATES["Todo"]},
                                 {"createdAt": ago(minutes=370), "actorId": USER, "toStateId": STATES["In Progress"]},
                                 {"createdAt": ago(minutes=360), "actorId": None, "toStateId": STATES["Todo"]}])
        self.assertEqual(out, "")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")

    def test_skipped_move_posts_no_comment(self):
        fake = Moved([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))], "In Review")
        self.assertEqual(self.plan(fake), "")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("In Review", []))
        self.assertNotIn("comments", fake.issues["TASK-1"])
        self.assertEqual(writes(fake), [("read", "TASK-1")])
        self.assertIn(f"recover: TASK-1: issue is {STATES['In Review']}", self.said())

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
            f.write("".join(lines))

    def stamp(self, **kw):
        return (NOW - timedelta(**kw)).astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def test_drops_old_lines(self):
        self.write(["pick: TASK-1 (3 in queue)\n",
                    f"{self.stamp(days=8)} start TASK-1 session=a transcript=x\n",
                    "recover: TASK-2 (last updated x)\n",
                    f"{self.stamp(days=6)} skip: queue empty\n",
                    "plan: new\n",
                    f"{self.stamp(hours=1)} start TASK-1 session=b transcript=x\n"])
        with mock.patch.object(router.drive.os, "replace", wraps=os.replace) as rep:
            router.prune(self.log, NOW)
        [(src, dst), _] = rep.call_args
        self.assertEqual((os.path.dirname(src), str(dst)), (self.tmp.name, self.log))
        with open(self.log) as f:
            self.assertEqual(f.read(), f"{self.stamp(days=6)} skip: queue empty\nplan: new\n"
                                       f"{self.stamp(hours=1)} start TASK-1 session=b transcript=x\n")
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["cfg", "runs.log", "transcripts"])

    def test_no_temp_left_when_the_rewrite_fails(self):
        self.write([f"{self.stamp(days=8)} skip: old\n", f"{self.stamp(hours=1)} skip: new\n"])
        with mock.patch.object(router.drive.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                router.prune(self.log, NOW)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["cfg", "runs.log", "transcripts"])
        with open(self.log) as f:
            self.assertEqual(len(f.readlines()), 2)

    def test_unchanged_file_not_rewritten(self):
        self.write([f"{self.stamp(days=1)} skip: queue empty\n"])
        with mock.patch.object(router.drive.os, "replace") as rep:
            router.prune(self.log, NOW)
        rep.assert_not_called()

    def test_missing_log_is_noop(self):
        router.prune(self.log, NOW)
        self.assertFalse(os.path.exists(self.log))


class Usage(unittest.TestCase):
    def test_unknown_flags_rejected(self):
        for argv in (["--help"], ["--prune", "x"], ["--gate", "new"], ["--gate", "resume"],
                     ["-h"], ["--issue", "TASK-1"],
                     ["--brake", "new"], ["--brake", "--dry-run"], ["--dry-run", "--brake"], ["--brake", "--brake"],
                     ["--tui", "--issue", "TASK-1"], ["--now", "--issue", "TASK-1", "--tui"], ["--tui", "--brake"],
                     ["--brake", "--tui"], ["--split", "right"], ["--now", "--beside", "dev"], ["--dry-run", "--split", "below"],
                     ["--tui", "--split"], ["--tui", "--beside", "--now"], ["--now", "--issue", "--dry-run"],
                     ["--tui", "--split", "right", "--split", "below"], ["--tui", "dev"], ["--split=right"],
                     ["--now", "--beside=dev"], ["--tui", "--split=right", "--split=below"],
                     ["--tui", "--beside=dev", "--beside", "dev"], ["--now", "--issue=TASK-1"], ["--tui", "--now=x"]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with redirect_stderr(err), mock.patch.object(attended, "layout", side_effect=AssertionError("layout ran")):
                    self.assertEqual(router.main(argv, gql=None, sh=mock.Mock(side_effect=AssertionError("probe ran"))), 2)
                self.assertEqual(err.getvalue(), router.USAGE + "\n")
        self.assertEqual(router.USAGE,
                         "usage: router.py [--now] [--dry-run] [--issue ID | --tui [--split right|below] [--beside SESSION]] | --brake")
        self.assertNotIn("--gate", router.USAGE + router.__doc__)


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
        self.assertEqual(self.said(), ["pick: TASK-2 (1 in queue)", "claim: TASK-2 task=deep-research"])
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

    def test_claim_skipped_when_no_longer_todo(self):
        fake = Moved([issue("TASK-1", "Todo", "researcher")], "In Review")
        self.assertEqual(self.claim(fake), "")
        self.assertIn("claim: TASK-1 is no longer Todo; skipping", self.err)
        self.assertEqual(fake.mutations, [])

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
        self.assertEqual(self.said(), ["pick: TASK-1 (2 in queue)", "claim: TASK-1 is no longer Todo; skipping",
                                       "pick: TASK-2 (2 in queue)", "claim: TASK-2 task=deep-research"])
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
        self.assertEqual(self.said(), ["pick: TASK-1 reached 4 attempts; In Review",
                                       f"pick: TASK-1: issue is {STATES['In Progress']}", "pick: nothing claimable"])

    def test_refused_claim_raises(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        fake.refuse.add("TASK-1")
        with self.assertRaises(RuntimeError) as cm:
            self.claim(fake)
        self.assertEqual(str(cm.exception), "issueUpdate: success: false")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_pick_recovers_then_claims(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))])
        self.assertEqual(self.claim(fake, recover=True), "TASK-1 https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(fake.issues["TASK-1"]["comments"][0], router.INTERRUPTED)


class Blockers(Base):
    def test_blocked_skipped_for_next_ready(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1, inverse=[
                               blocker("TASK-7"), blocker("TASK-8", "completed"), blocker("TASK-9", "unstarted")]),
                           issue("TASK-2", "Todo", "researcher", priority=2), issue("TASK-3", "Todo", "researcher", priority=3)])
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-7, TASK-9", "pick: TASK-2 (2 in queue)", "claim: TASK-2 task=deep-research"])
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_done_blockers(self):
        for states, blocked in ((["completed"], False), (["canceled"], False), (["duplicate"], False),
                                (["completed", "started"], True)):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker(f"TASK-{n}", s) for n, s in enumerate(states, 7)])])
            self.claim(fake)
            self.assertEqual(fake.issues["TASK-1"]["state"], "Todo" if blocked else "In Progress", states)
            self.assertEqual([m for m in self.said() if m.startswith("blocked:")], ["blocked: TASK-1 by TASK-8"] if blocked else [], states)

    def test_capped_blocked_stays_todo(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")])])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-7", "pick: queue empty"])
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("Todo", []))
        self.assertNotIn("comments", fake.issues["TASK-1"])

    def test_only_inverse_blocks_block(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=2,
                                 inverse=[blocker("TASK-5", kind="related"), blocker("TASK-6", kind="duplicate")]),
                           issue("TASK-2", "Todo", "researcher", priority=1, inverse=[blocker("TASK-1", "unstarted")])])
        self.assertEqual(self.claim(fake).split()[0], "TASK-1")
        self.assertEqual(self.said(), ["blocked: TASK-2 by TASK-1", "pick: TASK-1 (1 in queue)", "claim: TASK-1 task=deep-research"])
        self.assertFalse(any(re.search(r"\brelations\b", q) for q, _ in fake.queries))

    def test_null_blocker_unreadable(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker(None), blocker("TASK-7", "completed")])])
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(self.said(), ["blocked: TASK-1 by (unreadable)", "pick: queue empty"])

    def test_blocker_query_error_reads_per_issue(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1),
                           issue("TASK-2", "Todo", "researcher", priority=2, inverse=[blocker("TASK-7")]),
                           issue("TASK-3", "Todo", "researcher", priority=3, inverse=[blocker("TASK-8", "canceled")])])
        fake.todo_error, fake.unreadable = True, {"TASK-1"}
        self.assertEqual(self.claim(fake), "TASK-3 https://linear.app/x/TASK-3 Deep Research")
        said = self.said()
        self.assertRegex(said[0], r"^todo: blocker query failed, reading blockers per issue: linear api error: ")
        self.assertEqual(said[1:], ["blocked: TASK-1 by (unreadable)", "blocked: TASK-2 by TASK-7", "pick: TASK-3 (1 in queue)",
                                    "claim: TASK-3 task=deep-research"])
        todo = fake.reads()
        self.assertEqual(["inverseRelations" in q for q in todo], [True, False])
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
        for argv, out, last in (("claim", f"TASK-2 https://linear.app/x/TASK-2 {PD}", "pick: TASK-2 (1 in queue)"),
                                ("plan", "new", "plan: new (1 in queue)")):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7"), blocker("TASK-8", "backlog")]),
                               issue("TASK-2", "Todo", "pm", project=PD)])
            self.assertEqual(self.plan(fake, dry=True) if argv == "plan" else self.claim(fake, dry=True), out, argv)
            self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-7, TASK-8", last], argv)
            self.assertEqual(fake.mutations, [], argv)


def is_launch(cmd):
    return len(cmd) > 1 and cmd[1].endswith("run.py")


class FakeShell:
    """sessions: the tmux session names list-sessions prints; none = no tmux server (exit 1). fives: five_hour of the first
    probes, then probe_five. A run.py launch exits exits.get(ID, 0); exit 0 adds agent-pm-<role>-<ID> (role from --assignee)
    unless ID is in bounce."""
    def __init__(self, sessions=(), probe_five=0.2, fives=(), exits=None, bounce=()):
        self.calls, self.five, self.sessions = [], probe_five, list(sessions)
        self.fives, self.exits, self.bounce = list(fives), exits or {}, set(bounce)

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd == LIST:
            return subprocess.CompletedProcess(cmd, 0 if self.sessions else 1, stdout="".join(f"{n}\n" for n in self.sessions))
        if cmd[0] == "claude":
            five = self.fives.pop(0) if self.fives else self.five
            ev = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": five}, "seven_day": {"utilization": 0.1}}}}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(ev) + "\n")
        if is_launch(cmd):
            ident = cmd[cmd.index("--issue") + 1]
            rc = self.exits.get(ident, 0)
            if not rc and ident not in self.bounce:
                role = next(r for r, a in ROLE.items() if a == cmd[cmd.index("--assignee") + 1])
                self.sessions.append(config.session(role, ident))
            return subprocess.CompletedProcess(cmd, rc)
        return subprocess.CompletedProcess(cmd, 0)

    def launches(self):
        return [c for c in self.calls if is_launch(c)]


class Tick(Base):
    def test_hours_boundaries(self):
        for hour, runs in ((0, False), (1, True), (6, True), (7, False), (12, False), (23, False)):
            with self.subTest(hour=hour):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, hour=hour), 0)
                self.assertEqual(len(self.sh.launches()), int(runs))
                self.assertEqual("skip: outside hours" in self.err, not runs)
                self.assertEqual(self.sh.calls == [], not runs)

    def test_launchd_path_replaced(self):
        self.tick(FakeLinear([]))
        self.assertEqual(self.path, router.PATH)
        self.assertIn("/opt/homebrew/bin", self.path)

    def test_interrupted_text(self):
        self.assertEqual(router.INTERRUPTED, "The previous agent run was interrupted. Moving this issue back to the Todo queue.")

    def test_now_skips_hours_and_starts(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, "--now", hour=12)
        (launch,) = self.sh.launches()
        sid = launch[launch.index("--sid") + 1]
        self.assertEqual(launch[:2], [sys.executable, router.RUN])
        self.assertEqual(router.RUN, os.path.join(config.ROOT, "orchestrator", "src", "run.py"))
        self.assertEqual(launch[2:], ["--issue", "TASK-1", "--project", IDS[DR],
                                      "--assignee", ROLE["researcher"], "--sid", sid, "--task", "deep-research", "--mode", "new"])
        self.assertRegex(self.state, rf"start TASK-1 session={sid} task=deep-research\n$")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

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
            for argv in ((), ("--dry-run",), ("--now", "--issue", "TASK-1")):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv, shell=FakeShell(live)), 0)
                self.assertEqual(self.said(), ["skip: all roles full (engineer, pm, researcher)"], (cfg, argv))
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
        self.assertEqual(self.said(), ["live: engineer 1/2", "plan: new (2 in queue)", "pick: TASK-2 (2 in queue)",
                                       "claim: TASK-2 task=engineering", "launch TASK-2 (Product Design) exit=0"])
        self.assertEqual(self.launched_runs(), [("TASK-2", "new")])
        self.assertEqual({i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
                         {"TASK-1": ("In Progress", None), "TASK-2": ("In Progress", None), "TASK-3": ("Todo", None)})

    def test_todo_issue_with_live_session_not_claimed(self):
        self.config = self.write_config(CONFIG_ENG2)
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer", priority=1, project=PD),
                           issue("TASK-2", "Todo", "engineer", priority=2, project=PD)])
        self.tick(fake, shell=FakeShell(["agent-pm-engineer-TASK-1"]))
        self.assertEqual(self.said(), ["live: engineer 1/2", "plan: new (1 in queue)", "pick: TASK-2 (1 in queue)",
                                       "claim: TASK-2 task=engineering", "launch TASK-2 (Product Design) exit=0"])
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
        self.assertEqual(self.said(), ["live: engineer 1/1", "recover: TASK-3 reached 4 attempts; In Review",
                                       f"recover: TASK-2 (last updated {ago(hours=3)})", "plan: new (1 in queue)",
                                       "pick: TASK-5 (1 in queue)", "claim: TASK-5 task=product-design",
                                       "launch TASK-5 (Product Design) exit=0"])
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
        self.resumable("TASK-1", "a", 120)
        self.resumable("TASK-2", "b", 60)
        self.tick(fake, shell=FakeShell(["agent-pm-pm-TASK-9"]))
        self.assertEqual(self.said()[:2], ["live: pm 1/1", "plan: resume TASK-2 session=b"])
        self.assertEqual(self.launched_runs(), [("TASK-2", "resume")])
        self.assertEqual({i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
                         {"TASK-1": ("In Progress", None), "TASK-2": ("In Progress", None), "TASK-3": ("Todo", None)})
        self.assertNotIn("resume TASK-1", self.state)

    def test_nothing_to_do_with_full_role(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "In Progress", "engineer", updated=ago(hours=3))])
        self.tick(fake, shell=FakeShell(["agent-pm-engineer-TASK-2"]))
        self.assertEqual(self.said(), ["live: engineer 1/1", "plan: nothing to do", "skip: nothing to do"])
        self.assertEqual(self.sh.calls, [LIST])
        self.assertEqual([v["e"] for q, v in fake.queries if "users(filter" in q], [ROLE["researcher"], ROLE["pm"], ROLE["engineer"]])
        self.assertEqual(fake.mutations, [])

    def test_issue_flag_of_full_role_skips(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        for argv in (("--now", "--issue", "TASK-1"), ("--now", "--issue", "TASK-1", "--dry-run")):
            fake = FakeLinear([issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "Todo", "researcher")])
            self.assertEqual(self.tick(fake, *argv, shell=FakeShell(["agent-pm-engineer-TASK-5"])), 0)
            self.assertEqual(self.said(), ["live: engineer 1/1", "skip: TASK-1 belongs to full role engineer"], argv)
            self.assertEqual([v for _, v in fake.queries], [{"f": {"team": {"id": {"eq": TEAM}}, "id": {"eq": "TASK-1"}}}], argv)
            self.assertEqual(fake.mutations, [], argv)
            self.assertEqual(self.sh.calls, [LIST], argv)
            self.assertIn("TASK-8", self.state, argv)

    def test_issue_flag_of_not_full_role(self):
        for cfg, ident, live in ((CONFIG, "TASK-2", "agent-pm-engineer-TASK-5"), (CONFIG_ENG2, "TASK-1", "agent-pm-engineer-TASK-5")):
            self.config = self.write_config(cfg)
            fake = FakeLinear([issue("TASK-1", "Todo", "engineer", priority=1), issue("TASK-2", "Todo", "researcher", priority=4)])
            self.tick(fake, "--now", "--issue", ident, shell=FakeShell([live]))
            self.assertEqual(self.launched(), ident, cfg)
            self.assertEqual({i: t["state"] for i, t in fake.issues.items()},
                             {"TASK-1": "Todo", "TASK-2": "Todo", ident: "In Progress"}, cfg)

    def test_issue_flag_with_live_session_skips(self):
        self.config = self.write_config(CONFIG_ENG2)
        for argv in (("--now", "--issue", "TASK-1"), ("--now", "--issue", "TASK-1", "--dry-run")):
            fake = FakeLinear([issue("TASK-1", "Todo", "engineer")])
            self.assertEqual(self.tick(fake, *argv, shell=FakeShell(["agent-pm-engineer-TASK-1"])), 0)
            self.assertEqual(self.said(), ["live: engineer 1/2", "skip: TASK-1 has a live session"], argv)
            self.assertEqual((fake.queries, fake.mutations, self.sh.calls), ([], [], [LIST]), argv)

    def test_issue_flag_not_a_role_issue_while_full(self):
        for ident in ("TASK-9", "TASK-2", "TASK-3"):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher"), issue("TASK-2", "Todo", "someone"), issue("TASK-3", "Todo")])
            self.tick(fake, "--now", "--issue", ident, shell=FakeShell(["agent-pm-engineer-TASK-5"]))
            self.assertIn(f"pick: {ident} is not a Todo issue assigned to a role account", self.err)
            self.assertEqual((self.sh.launches(), fake.mutations), ([], []))

    def test_one_run_per_tick_resume_first(self):
        self.config = self.write_config(CONFIG_ENG2)
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", priority=3),
                           issue("TASK-8", "In Progress", "engineer", priority=4, project=PD),
                           issue("TASK-2", "Todo", "researcher", priority=1), issue("TASK-3", "Todo", "pm", priority=2, project=PD),
                           issue("TASK-4", "Todo", "engineer", priority=2, project=PD)], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.resumable("TASK-8", "h", 70)
        self.tick(fake)
        self.assertEqual(self.said(), ["plan: resume TASK-1 session=a", "launch TASK-1 (Deep Research) exit=0"])
        self.assertEqual(self.launched_runs(), [("TASK-1", "resume")])
        self.assertEqual({i: t["state"] for i, t in fake.issues.items()},
                         {"TASK-1": "In Progress", "TASK-8": "In Progress", "TASK-2": "Todo", "TASK-3": "Todo", "TASK-4": "Todo"})
        self.assertEqual((self.probes(), self.sh.calls.count(LIST)), (1, 1))

    def test_one_claim_per_tick(self):
        self.config = self.write_config(CONFIG_ENG2)
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "pm", priority=2, project=PD),
                           issue("TASK-3", "Todo", "engineer", priority=2, project=PD)])
        self.tick(fake)
        self.assertEqual(self.said(), ["plan: new (3 in queue)", "pick: TASK-1 (3 in queue)", "claim: TASK-1 task=deep-research",
                                       "launch TASK-1 (Deep Research) exit=0"])
        self.assertEqual([c[3] if is_launch(c) else c[0] for c in self.sh.calls], ["tmux", "claude", "TASK-1"])
        self.assertEqual({i: t["state"] for i, t in fake.issues.items()}, {"TASK-1": "In Progress", "TASK-2": "Todo", "TASK-3": "Todo"})

    def test_issue_flag_launches_only_that_issue(self):
        for dry in (False, True):
            self.lines, self.hist = [], {}
            fake = FakeLinear([issue("TASK-1", "In Progress", "researcher"), issue("TASK-2", "Todo", "researcher", priority=1),
                               issue("TASK-3", "Todo", "pm", priority=2, project=PD),
                               issue("TASK-4", "Todo", "engineer", priority=3, project=PD)], self.hist)
            self.resumable("TASK-1", "a", 60)
            self.tick(fake, "--now", "--issue", "TASK-4", *["--dry-run"] * dry)
            said = self.said()
            self.assertEqual(self.probes(), 1, dry)
            self.assertNotIn("resume TASK-1", self.state, dry)
            self.assertEqual(len(fake.reads()), 1, dry)
            self.assertFalse(any("id" in v["f"] for q, v in fake.queries if "issues(filter" in q), dry)
            if dry:
                self.assertEqual(said[0], "pick: TASK-4 (1 in queue)")
                self.assertRegex(said[1], r"^usage: status=allowed .* \(1 planned, allowed\)$")
                self.assertEqual((len(said), self.sh.launches(), fake.mutations), (2, [], []))
                continue
            self.assertEqual(said, ["pick: TASK-4 (1 in queue)", "claim: TASK-4 task=engineering",
                                    "launch TASK-4 (Product Design) exit=0"])
            self.assertEqual(self.launched_runs(), [("TASK-4", "new")])
            self.assertEqual({i: t["state"] for i, t in fake.issues.items()},
                             {"TASK-1": "In Progress", "TASK-2": "Todo", "TASK-3": "Todo", "TASK-4": "In Progress"})

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
        self.assertEqual(said[:-1], ["plan: new (4 in queue)", "pick: TASK-3 reached 4 attempts; In Review", "pick: TASK-5 (4 in queue)"])
        self.assertRegex(said[-1], r"^usage: status=allowed .* \(1 planned, allowed\)$")
        self.assertEqual(self.sh.calls, [LIST, router.PROBE])
        self.assertEqual((fake.mutations, self.state), ([], before))
        self.assertNotIn(RECHECK, [q for q, _ in fake.queries])

    def test_dry_run_usage_blocked(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, "--dry-run", shell=FakeShell(probe_five=0.95))
        self.assertEqual(self.said()[:-1], ["plan: new (1 in queue)", "pick: TASK-1 (1 in queue)"])
        self.assertRegex(self.said()[-1], r"^usage: status=allowed five_hour=0.95 .* \(1 planned, blocked\)$")

    def test_dry_run(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "Todo", "researcher")])
        self.tick(fake, "--dry-run", hour=12, shell=FakeShell(["agent-pm-pm-TASK-5", "agent-pm-engineer-TASK-6"]))
        said = self.said()
        self.assertEqual(said[:4], ["skip: outside hours", "live: engineer 1/1, pm 1/1", "plan: new (1 in queue)",
                                    "pick: TASK-2 (1 in queue)"])
        self.assertRegex(said[4], r"^usage: status=allowed .*\(1 planned, allowed\)$")
        self.assertEqual(len(said), 5)
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
        self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-2", "blocked: TASK-2 by TASK-1", "blocked: TASK-3 by TASK-9",
                                       "plan: nothing to do", "skip: nothing to do"])
        self.assertEqual(([c[0] for c in self.sh.calls].count("claude"), self.sh.launches(), fake.mutations), (0, [], []))
        self.assertEqual(len(fake.reads()), 1)
        fake = FakeLinear(issues())
        self.assertEqual(self.tick(fake, "--dry-run"), 0)
        self.assertEqual(self.said()[3], "plan: nothing to do")
        self.assertRegex(self.said()[4], r"^usage: .* \(0 planned, allowed\)$")
        self.assertEqual(len(self.said()), 5)
        self.assertEqual(([c[0] for c in self.sh.calls].count("claude"), self.sh.launches(), fake.mutations), (1, [], []))

    def test_blocked_issue_flag_skips_before_probe(self):
        for argv in (("--now", "--issue", "TASK-1"), ("--now", "--issue", "TASK-1", "--dry-run")):
            for resume in (False, True):
                self.lines = []
                issues = [issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")]), issue("TASK-4", "Todo", "pm", project=PD)]
                if resume:
                    issues.append(issue("TASK-2", "In Progress", "researcher"))
                    self.resumable("TASK-2", "a", 60)
                fake = FakeLinear(issues, self.hist)
                before = self.unmap("\n".join(self.lines) + ("\n" if self.lines else ""))
                self.assertEqual(self.tick(fake, *argv), 0, (argv, resume))
                self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-7"], (argv, resume))
                self.assertEqual(([c[0] for c in self.sh.calls].count("claude"), self.sh.launches(), fake.mutations), (0, [], []), (argv, resume))
                self.assertEqual(self.state, before, (argv, resume))
                self.assertEqual(len(fake.reads()), 1, (argv, resume))

    def test_ready_tick_reads_todo_once(self):
        for argv, runs in (((), [("TASK-2", "new")]), (("--now", "--issue", "TASK-2"), [("TASK-2", "new")])):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1, inverse=[blocker("TASK-7")]),
                               issue("TASK-2", "Todo", "researcher", priority=2), issue("TASK-3", "Todo", "pm", priority=3, project=PD)])
            self.assertEqual(self.tick(fake, *argv), 0, argv)
            self.assertEqual(self.launched_runs(), runs, argv)
            self.assertEqual(len(fake.reads()), 1, argv)
            self.assertEqual("plan: new (2 in queue)" in self.said(), not argv, argv)
            self.assertEqual(self.said().count("blocked: TASK-1 by TASK-7"), 1, argv)

    def test_usage_blocked(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, shell=FakeShell(probe_five=0.95))
        self.assertIn("skip: new blocked by usage", self.err)
        self.assertEqual(self.sh.launches(), [])
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_resume(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.tick(fake)
        self.assertEqual([c for c in self.sh.calls if c[0] == "claude"], [router.PROBE])
        self.assertNotIn(" n=", self.state.splitlines()[-1])
        (launch,) = self.sh.launches()
        self.assertEqual(launch[2:8], ["--issue", "TASK-1", "--project", IDS[DR],
                                        "--assignee", ROLE["researcher"]])
        self.assertEqual(launch[-2:], ["--mode", "resume"])
        self.assertIn("--sid", launch)
        self.assertTrue(self.state.endswith("resume TASK-1 session=a task=deep-research\n"))

    def test_issue_flag_still_recovers(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)), issue("TASK-2", "Todo", "researcher")])
        self.tick(fake, "--now", "--issue", "TASK-1")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.INTERRUPTED])
        (launch,) = self.sh.launches()
        self.assertEqual(launch[launch.index("--issue") + 1], "TASK-1")

    def test_prune_runs_before_plan(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        self.tick(FakeLinear([]))
        self.assertNotIn("TASK-8", self.state)

    def test_prune_failure_continues(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with mock.patch.object(router, "prune", side_effect=OSError("disk")):
            self.tick(fake)
        self.assertIn("skip: prune failed", self.err)
        self.assertEqual(len(self.sh.launches()), 1)

    def test_start_line(self):
        sid = self.sid("a")
        self.assertEqual(router.start_line("TASK-1", sid, "deep-research"), f"start TASK-1 session={sid} task=deep-research")

    def test_a_logged_transcript_field_still_parses(self):
        self.lines.append(f"{(NOW - timedelta(minutes=5)).astimezone():%Y-%m-%d %H:%M:%S} start TASK-1 session=s "
                          "transcript=/p/x.jsonl task=light-research")
        with open(self.log, "w") as f:
            f.write("\n".join(self.lines) + "\n")
        self.assertEqual([e[1:] for e in router.parse_log(self.log)], [("start", "TASK-1", "s", "light-research")])

    def test_resume_finds_the_transcript_under_the_recorded_cwd(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")], self.hist)
        self.moved("TASK-1", 61)
        self.add("start", "TASK-1", "a", 60)
        os.makedirs(config.run_dir("TASK-1"), exist_ok=True)
        with open(os.path.join(config.run_dir("TASK-1"), "run.json"), "w") as f:
            json.dump({"sessions": [{"sid": self.sid("a"), "cwd": self.tmp.name, "project": False}]}, f)
        self.addCleanup(os.remove, os.path.join(config.run_dir("TASK-1"), "run.json"))
        self.touch("TASK-1", "a", 40)
        self.assertTrue(config.transcript("TASK-1", self.sid("a"), self.tdir).startswith(
            os.path.join(self.tdir, re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(self.tmp.name)))))
        self.assertEqual(self.plan(fake), f"resume TASK-1 a {fake.issues['TASK-1']['url']} {DR}")

    def test_driver_session_counts_toward_max_runs_and_a_tui_session_does_not(self):
        tui = drive.tui_session("engineer", "engineering", self.sid("a"))
        for live, runs in (([tui, "agent-pm-engineer-TASK-9"], []), ([tui], [("TASK-1", "new")])):
            with self.subTest(live=live):
                self.tick(FakeLinear([issue("TASK-1", "Todo", "engineer")]), shell=FakeShell(live))
                self.assertEqual(self.launched_runs(), runs)
        self.assertEqual(router.live_sessions(["engineer"], FakeShell([tui, "agent-pm-engineer-TASK-9"])), {"engineer": ["TASK-9"]})


class TuiTick(Base):
    """router.py --tui: attended.layout (patched) before the tick; the tick's run.py launch gains --runner=tui."""
    def tui_tick(self, fake, *argv, layout=None, **kw):
        place = mock.Mock(side_effect=layout, return_value=drive.Layout())
        with mock.patch.object(attended, "layout", place):
            rc = self.tick(fake, *argv, **kw)
        self.layout = place.call_args_list
        return rc

    def test_new_run_launch_argv(self):
        rows = ((("--now", "--tui"), None, None, ["--runner=tui"]),
                (("--tui", "--beside", "dev", "--now", "--split", "below"), "below", "dev",
                 ["--runner=tui", "--split=below", "--beside=dev"]),
                (("--beside", "-x", "--tui"), None, "-x", ["--runner=tui", "--beside=-x"]),
                (("--split", "right", "--tui"), "right", None, ["--runner=tui", "--split=right"]),
                (("--now", "--tui", "--split=below", "--beside=dev"), "below", "dev",
                 ["--runner=tui", "--split=below", "--beside=dev"]),
                (("--beside=dev", "--tui", "--split", "right"), "right", "dev", ["--runner=tui", "--split=right", "--beside=dev"]))
        for argv, split, beside, tail in rows:
            with self.subTest(argv=argv):
                self.lines = []
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tui_tick(fake, *argv), 0)
                (launch,) = self.sh.launches()
                sid = launch[launch.index("--sid") + 1]
                self.assertEqual(launch, [sys.executable, router.RUN, "--issue", "TASK-1", "--project", IDS[DR],
                                          "--assignee", ROLE["researcher"], "--sid", sid, "--task", "deep-research",
                                          "--mode", "new", *tail])
                self.assertTrue(self.state.endswith(" " + router.start_line("TASK-1", sid, "deep-research") + "\n"))
                self.assertEqual(self.layout, [mock.call(split, beside)])
                self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_resume_launch_argv(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.tui_tick(fake, "--tui", "--split", "below")
        (launch,) = self.sh.launches()
        self.assertEqual(launch[-4:], ["--mode", "resume", "--runner=tui", "--split=below"])
        self.assertTrue(self.state.endswith(" resume TASK-1 session=a task=deep-research\n"))

    def test_dry_run_launches_nothing(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tui_tick(fake, "--tui", "--dry-run", "--now")
        self.assertEqual(self.said()[:2], ["plan: new (1 in queue)", "pick: TASK-1 (1 in queue)"])
        self.assertEqual((self.sh.launches(), fake.mutations, self.state), ([], [], ""))
        self.assertEqual(self.layout, [mock.call(None, None)])

    def test_no_place_for_the_pane_exits_2_before_the_tick(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        before = self.unmap("\n".join(self.lines) + "\n")
        for argv in (("--tui", "--beside", "gone"), ("--now", "--tui", "--dry-run", "--beside", "gone")):
            with self.subTest(argv=argv):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tui_tick(fake, *argv, layout=attended.Bad("no tmux session gone")), 2)
                self.assertEqual(self.err, "router.py: no tmux session gone\n")
                self.assertEqual((fake.queries, self.sh.calls, self.state), ([], [], before))

    def test_without_tui_no_layout_and_a_headless_launch(self):
        for argv in ((), ("--now",), ("--now", "--issue", "TASK-1")):
            with self.subTest(argv=argv):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                with mock.patch.object(attended, "layout", side_effect=AssertionError("layout ran")):
                    self.tick(fake, *argv)
                (launch,) = self.sh.launches()
                self.assertEqual(launch[-2:], ["--mode", "new"])


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
        absent = "is not in orchestrator/config.toml's [task_labels]. Fix the label, then move the issue back to Todo."
        cases = (([], ("deep-research", None)),
                 (outside, ("deep-research", None)),
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
        self.assertIn("claim: TASK-1 bad task label; In Review", self.said())
        self.assertEqual(self.launched(), "TASK-2")
        self.assertEqual([line.split()[2:4] for line in self.state.splitlines()], [["start", "TASK-2"]])
        self.assertTrue(self.state.endswith(" task=deep-research\n"))

    def test_capped_and_bad_label_todo_skipped_for_the_next(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1),
                           issue("TASK-2", "Todo", "researcher", priority=2, labels=[label("Orphan", ORPHAN_LABEL)]),
                           issue("TASK-3", "Todo", "pm", priority=3, project=PD),
                           issue("TASK-4", "Todo", "engineer", priority=4, project=PD)])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.tick(fake)
        self.assertEqual(self.said(), ["plan: new (4 in queue)", "pick: TASK-1 reached 4 attempts; In Review",
                                       "pick: TASK-2 (4 in queue)", "claim: TASK-2 bad task label; In Review",
                                       "pick: TASK-3 (4 in queue)", "claim: TASK-3 task=product-design",
                                       "launch TASK-3 (Product Design) exit=0"])
        self.assertEqual({i: fake.issues[i].get("comments") for i in ("TASK-1", "TASK-2")},
                         {"TASK-1": [router.CAP_COMMENT], "TASK-2": [self.ORPHAN]})
        self.assertEqual([i for op, i in writes(fake) if op == "read"], ["TASK-1", "TASK-2"])
        self.assertEqual([v["i"] for q, v in fake.queries if q == RECHECK], ["TASK-2", "TASK-3"])
        self.assertEqual(fake.issues["TASK-4"]["state"], "Todo")

    def test_blocked_invalid_label_waits_for_its_blocker(self):
        for argv, skip, plan in (((), ["plan: nothing to do", "skip: nothing to do"], ["plan: new (1 in queue)"]),
                                 (("--now", "--issue", "TASK-1"), [], [])):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", labels=[label("Orphan", ORPHAN_LABEL)], inverse=[blocker("TASK-7")])])
            self.tick(fake, *argv)
            self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-7"] + skip, argv)
            self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations, self.sh.launches()), ("Todo", [], []), argv)
            self.assertNotIn(RECHECK, [q for q, _ in fake.queries], argv)
            fake.issues["TASK-1"]["inverseRelations"]["nodes"][0]["issue"]["state"]["type"] = "completed"
            self.tick(fake, *argv)
            t = fake.issues["TASK-1"]
            self.assertEqual((t["state"], t["comments"]), ("In Review", [self.ORPHAN]), argv)
            self.assertEqual(self.said(), plan + ["pick: TASK-1 (1 in queue)", "claim: TASK-1 bad task label; In Review",
                                                  "pick: nothing claimable", "skip: nothing claimed"], argv)
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
        self.assertIn("claim: TASK-1 task=light-research", self.said())
        self.assertEqual(self.sh.launches()[0][-4:], ["--task", "light-research", "--mode", "new"])
        self.assertNotIn("labels", fake.reads()[0])

    def test_fr8_dry_run_resolves_nothing(self):
        for argv in (False, True):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", labels=[label("Orphan", ORPHAN_LABEL)])])
            self.assertEqual(self.claim(fake, dry=True, recover=argv), "TASK-1 https://linear.app/x/TASK-1 Deep Research", argv)
            self.assertEqual(self.said(), ["pick: TASK-1 (1 in queue)"], argv)
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

    def test_fr9_fr13_resume_passes_the_recorded_task(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")], self.hist)
        self.moved("TASK-1", 61)
        self.add("resume", "TASK-1", "a", 60, "light-research")
        self.add("resume", "TASK-1", "a", 50)
        self.touch("TASK-1", "a", 40)
        self.tick(fake)
        (launch,) = self.sh.launches()
        self.assertEqual(launch[launch.index("--sid"):], ["--sid", self.sid("a"), "--task", "light-research", "--mode", "resume"])
        self.assertTrue(self.state.endswith(" resume TASK-1 session=a task=light-research\n"))
        self.assertIn("plan: resume TASK-1 session=a", self.said())

    def test_fr14_no_record_resumes_the_assignee_roles_default(self):
        for role, project, task in (("researcher", DR, "deep-research"), ("pm", PD, "product-design")):
            self.lines, self.hist = [], {}
            fake = FakeLinear([issue("TASK-1", "In Progress", role, project=project, labels=[label("Light Research", LIGHT)])], self.hist)
            self.resumable("TASK-1", "a", 60)
            self.tick(fake)
            (launch,) = self.sh.launches()
            self.assertEqual(launch[-4:], ["--task", task, "--mode", "resume"], role)
            self.assertTrue(self.state.endswith(f" resume TASK-1 session=a task={task}\n"), role)

    def test_fr15_recorded_task_no_longer_the_roles_goes_to_review(self):
        for role, project, task, tasks in (("researcher", DR, "orphan", "deep-research, light-research"),
                                           ("pm", PD, "light-research", "product-design")):
            self.lines, self.hist = [], {}
            fake = FakeLinear([issue("TASK-1", "In Progress", role, project=project)], self.hist)
            self.resumable("TASK-1", "a", 60, task)
            before = self.unmap("\n".join(self.lines) + "\n")
            self.tick(fake)
            t = fake.issues["TASK-1"]
            self.assertEqual((t["state"], t["assignee"], t["comments"], t["subscribers"]),
                             ("In Review", who(role), [f'The interrupted agent run\'s task "{task}" is not one of {role}\'s tasks ({tasks}); needs a look.'],
                              ["me@x.com"]), role)
            self.assertEqual(writes(fake), [("issueSubscribe", "TASK-1"), ("read", "TASK-1"), ("issueUpdate", "TASK-1"),
                                            ("commentCreate", "TASK-1")], role)
            self.assertEqual(fake.mutations[1][1]["s"], STATES["In Review"], role)
            self.assertEqual(self.said(), [f"recover: TASK-1 task={task} is not one of {role}'s tasks; In Review",
                                           "plan: nothing to do", "skip: nothing to do"], role)
            self.assertEqual((self.sh.launches(), self.state), ([], before), role)

    def test_fr15_every_resumable_issue_is_checked(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", priority=1),
                           issue("TASK-2", "In Progress", "researcher", priority=2)], self.hist)
        self.resumable("TASK-1", "a", 60, "light-research")
        self.resumable("TASK-2", "b", 60, "orphan")
        self.assertEqual(self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-2"]["state"]), ("In Progress", "In Review"))
        self.assertIn("recover: TASK-2 task=orphan is not one of researcher's tasks; In Review", self.said())

    def test_fr16_requeued_issue_resolved_again_at_claim(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", labels=[label("Light Research", LIGHT)])], self.hist)
        self.moved("TASK-1", 61)
        self.add("start", "TASK-1", "a", 60, "deep-research")
        self.tick(fake)
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.INTERRUPTED])
        self.assertIn("claim: TASK-1 task=light-research", self.said())
        (launch,) = self.sh.launches()
        self.assertEqual(launch[-4:], ["--task", "light-research", "--mode", "new"])
        self.assertTrue(self.state.endswith(" task=light-research\n"))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")


if __name__ == "__main__":
    unittest.main()
