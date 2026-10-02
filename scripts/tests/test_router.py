import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board_ids import HEADER, STATES as IDS_BY_KEY, TASK_GROUP, TEAM, team_node  # noqa: E402
import pipeline  # noqa: E402
import router  # noqa: E402

NOW = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
AGENT, USER = "agent", "user"   # history actor ids: an agent account, a human_members user
STATES = {"Todo": IDS_BY_KEY["todo"], "In Progress": IDS_BY_KEY["in_progress"], "In Review": IDS_BY_KEY["in_review"]}
NAMES = {i: n for n, i in STATES.items()}
DR, PD = "Deep Research", "Product Design"
IDS = {DR: "p-dr", PD: "p-pd"}
ROLE = {name: r.account for name, r in pipeline.registry()[0].items()}  # the repo's roles/
CONFIG = HEADER + '[roles.researcher]\nnext = "pm"\n[roles.pm]\nnext = "engineer"\n[roles.engineer]\n'
LIGHT, DEEP, STRAY, ORPHAN_LABEL = (f"00000000-0000-4000-8000-0000000000{n}" for n in (21, 22, 23, 24))   # label ids; STRAY maps to no task
TASK_LABELS = f'[task_labels]\nlight-research = "{LIGHT}"\ndeep-research = "{DEEP}"\norphan = "{ORPHAN_LABEL}"\n'
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
        # the task_label_group's issueLabel node, its children the labels TaskLabels maps
        self.group = {"isGroup": True, "children": {"nodes": [{"id": i} for i in (LIGHT, DEEP, ORPHAN_LABEL)]}}

    def __call__(self, query, **v):
        self.queries.append((query, v))
        if query == pipeline.Q_TEAM:
            return {"teams": {"nodes": [team_node()]}}
        if query == pipeline.Q_TASK_GROUP:
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
            else:
                upd = v.get("u") or {"stateId": v["s"]}
                issue["state"] = NAMES[upd["stateId"]]
                if "assigneeId" in upd:
                    issue["assignee"] = upd["assigneeId"]
            return {}
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


def ops(fake):
    return [re.search(r"\{ (\w+)\(", q).group(1) for q, _ in fake.mutations]


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
        self.root = pipeline.ROOT

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
        path = os.path.join(self.tmp.name, "cfg", "pipeline.toml")
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
        extra = "n=1" if kind == "resume" else f"transcript={pipeline.transcript(ident, s, self.tdir)}"
        self.lines.append(f"{ts} {kind} {ident} session={s} {extra}" + (f" task={task}" if task else ""))

    def touch(self, ident, sid, minutes_ago, sub=None):
        """<sid>.jsonl, or sub under <sid>/, in the issue's transcript folder."""
        p = pipeline.transcript(ident, self.sid(sid), self.tdir)
        if sub:
            p = os.path.join(p[:-len(".jsonl")], sub)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
        t = (NOW - timedelta(minutes=minutes_ago)).timestamp()
        os.utime(p, (t, t))

    def run_main(self, fake, *argv):
        with open(self.log, "w") as f:
            f.write("\n".join(self.lines) + "\n")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = router.main(list(argv) + [self.log], gql=fake, now=NOW, tdir=self.tdir, config=self.config, root=self.root)
        self.err = self.unmap(err.getvalue())
        return rc, self.unmap(out.getvalue().strip())

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
        self.assertEqual(router.gate("new", ['{"type":"system"}', "garbage"]), (False, "no rate_limit_event", None))

    def test_returns_five_hour(self):
        self.assertEqual(router.gate("new", [event(five=0.9), "{}", event(five=0.65, seven_day=0.2)]),
                         (True, "status=allowed five_hour=0.65 seven_day=0.2", 0.65))
        no_five = json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {}}})
        self.assertEqual(router.gate("new", [no_five]), (False, "status=allowed five_hour=None", None))

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
                self.assertEqual(router.gate("new", lines)[0], ok)

    def test_main_reads_stdin(self):
        for kind, text, rc, said in (("resume", event(five=0.5, seven_day=0.3), 0, "status=allowed five_hour=0.5 seven_day=0.3"),
                                     ("new", event(five=0.7, seven_day=0.3), 0, "status=allowed five_hour=0.7 seven_day=0.3"),
                                     ("new", event(five=0.9), 1, "status=allowed five_hour=0.9"),
                                     ("new", '{"type":"system"}', 1, "no rate_limit_event")):
            with self.subTest(said=said):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(router.main(["--gate", kind], gql=None, stdin=io.StringIO(text + "\n")), rc)
                self.assertEqual(out.getvalue(), said + "\n")


class Plan(Base):
    def test_nothing(self):
        fake = FakeLinear([issue("TASK-1", "In Review", "researcher")])
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))

    def test_new(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.run_main(fake, "--plan")[1], "new")
        self.assertEqual(fake.mutations, [])

    def test_attempt_cap_beats_resume(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=5)))
        self.resumable("TASK-1", "sid1", 300)
        for m in (200, 150, 100):
            self.add("resume", "TASK-1", "sid1", m)
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])

    def test_live_session_not_resumed_or_recovered(self):
        resume = "resume TASK-3 c 1 https://linear.app/x/TASK-3 Deep Research"
        for others, out in (((), ""), ((issue("TASK-2", "Todo", "researcher"),), "new"),
                            ((issue("TASK-3", "In Progress", "researcher", priority=3),), resume)):
            with self.subTest(out=out):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=1, updated=ago(hours=5)), *others)
                self.resumable("TASK-1", "a", 300)
                self.touch("TASK-1", "a", 5, "subagents/workflows/r/journal.jsonl")
                if any(i["state"] == "In Progress" for i in others):
                    self.resumable("TASK-3", "c", 100)
                self.assertEqual(self.run_main(fake, "--plan"), (0, out))
                self.assertEqual(fake.mutations, [])

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
                self.assertEqual(self.run_main(fake, "--plan")[1], f"resume TASK-2 task-2 1 https://linear.app/x/TASK-2 {project}")

    def test_recover_returns_every_candidate_in_order(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=3), issue("TASK-2", "In Progress", "pm", priority=1, project=PD),
                         issue("TASK-3", "In Progress", "researcher", priority=3))
        self.resumable("TASK-1", "a", 100)
        self.resumable("TASK-2", "b", 50, "product-design")
        self.resumable("TASK-3", "c", 200)
        self.add("resume", "TASK-3", "c", 150)
        self.assertEqual(self.run_main(fake, "--plan")[1], f"resume TASK-2 b 1 https://linear.app/x/TASK-2 {PD}")
        board = router.Board(fake, router.parse_log(self.log), self.tdir, NOW, True, pipeline.load_config(self.config))
        self.assertEqual([(i["identifier"], self.names[s], k, task) for i, s, k, task in board.recover()],
                         [("TASK-2", "b", 1, "product-design"), ("TASK-3", "c", 2, "deep-research"), ("TASK-1", "a", 1, "deep-research")])
        self.assertEqual(fake.mutations, [])

    def test_no_current_sid_never_outranks(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=1), issue("TASK-2", "In Progress", "researcher", priority=4))
        self.add("start", "TASK-1", "old", 400)
        self.touch("TASK-1", "old", 300)
        self.moved("TASK-1", 60, actor=USER)
        self.resumable("TASK-2", "b", 100)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-2 b 1 https://linear.app/x/TASK-2 Deep Research")

    def test_sid_without_start_sorts_by_first_line(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=2), issue("TASK-2", "In Progress", "researcher", priority=2))
        self.add("resume", "TASK-1", "a", 300)
        self.add("resume", "TASK-1", "a", 50)
        self.touch("TASK-1", "a", 40)
        self.resumable("TASK-2", "b", 200)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 a 3 https://linear.app/x/TASK-1 Deep Research")

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
        resume = "resume TASK-3 c 1 https://linear.app/x/TASK-3 Deep Research"
        for argv, out in ((("--plan",), resume), (("--plan", "--dry-run"), resume), (("--pick",), "TASK-9 https://linear.app/x/TASK-9")):
            with self.subTest(argv=argv):
                self.lines, self.hist = [], {}
                fake = self.walk_fixture()
                self.assertEqual(self.run_main(fake, *argv)[1], out)
                self.assertIn("recover: TASK-1 session=a has no transcript", self.err)
                self.assertIn("recover: TASK-5 reached 4 attempts", self.err)
                if "--dry-run" in argv:
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
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.mutations, [])
        self.lines = []
        self.add("start", "TASK-1", "a", 40)
        self.assertEqual(self.run_main(fake, "--plan")[1], "new")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("Todo", who("researcher")))

    def test_no_sid_keeps_two_hour_rule(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "In Progress", "engineer", updated=ago(hours=3), project=PD),
                           issue("TASK-2", "In Progress", "researcher", updated=ago(hours=1))])
        self.run_main(fake, "--plan")
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
        self.assertEqual(self.run_main(fake, "--plan")[1], "new")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_tolerance(self):
        for start, want in ((100.05, "resume"), (106, "")):
            self.lines, self.hist = [], {}
            fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
            self.moved("TASK-1", 100)
            self.add("start", "TASK-1", "a", start)
            self.touch("TASK-1", "a", 40)
            self.assertEqual(self.run_main(fake, "--plan")[1].split(" ")[0], want)

    def test_no_move_in_history_is_current(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.add("start", "TASK-1", "a", 100)
        self.touch("TASK-1", "a", 40)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 a 1 https://linear.app/x/TASK-1 Deep Research")

    def test_no_current_sid_never_capped(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=1)))
        for m in (600, 550, 500, 450):
            self.add("start", "TASK-1", f"s{m}", m)
        self.moved("TASK-1", 60, actor=USER)
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.mutations, [])

    def test_launch_failures_end_in_review(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        for n in range(4):
            self.moved("TASK-1", 41)
            self.add("start", "TASK-1", f"s{n}", 40)
            if n < 3:
                self.assertEqual(self.run_main(fake, "--plan")[1], "new")
                self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")
                self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-1 https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
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
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 new 2 https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(fake.mutations, [])

    def test_user_reset_orders_by_new_sid(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=2), issue("TASK-2", "In Progress", "researcher", priority=2))
        self.reset_fixture()
        self.resumable("TASK-2", "b", 200)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-2 b 1 https://linear.app/x/TASK-2 Deep Research")

    def test_recover_skips_other_assignees(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "someone", updated=ago(hours=3)),
                           issue("TASK-2", "In Progress", None, updated=ago(hours=3))])
        self.run_main(fake, "--plan")
        self.assertEqual(fake.mutations, [])

    def test_attempt_cap_subscribes_humans(self):
        for members in ([], ["me@x.com", "b@x.com"]):
            with self.subTest(members=members):
                self.config = self.write_config((f"human_members = {json.dumps(members)}\n" if members else "") + CONFIG)
                fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=40))])
                self.lines = []
                for i, sid in enumerate("abcd"):
                    self.add("start", "TASK-1", sid, 400 - i * 50)
                self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
                t = fake.issues["TASK-1"]
                self.assertEqual((t["state"], t["assignee"], t["comments"], t.get("subscribers", [])),
                                 ("In Review", who("researcher"), [router.CAP_COMMENT], members))
                self.assertEqual(ops(fake), ["issueSubscribe"] * len(members) + ["commentCreate", "issueUpdate"])
                self.assertEqual(fake.mutations[-1][1]["u"], {"stateId": STATES["In Review"]})

    def test_unknown_human_fails_loud(self):
        self.config = self.write_config('human_members = ["nobody@x.com"]\n' + CONFIG)
        with self.assertRaises(SystemExit) as e:
            self.run_main(FakeLinear([]), "--plan")
        self.assertIn("nobody@x.com", str(e.exception))

    def capped(self, hist):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))], {"TASK-1": hist})
        for i, sid in enumerate("abcd"):
            self.add("start", "TASK-1", sid, 400 - i * 50)
        self.touch("TASK-1", "d", 200)
        return fake, self.run_main(fake, "--plan")[1]

    def test_attempt_cap_reset_by_user(self):
        fake, out = self.capped([{"createdAt": ago(minutes=380), "actorId": USER, "toStateId": STATES["Todo"]}])
        self.assertEqual(out, "resume TASK-1 d 1 https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(fake.mutations, [])

    def test_attempt_cap_reset_only_by_human_members(self):
        fake, out = self.capped([{"createdAt": ago(minutes=380), "actorId": AGENT, "toStateId": STATES["Todo"]},
                                 {"createdAt": ago(minutes=375), "actorId": "role-account", "toStateId": STATES["Todo"]},
                                 {"createdAt": ago(minutes=370), "actorId": USER, "toStateId": STATES["In Progress"]},
                                 {"createdAt": ago(minutes=360), "actorId": None, "toStateId": STATES["Todo"]}])
        self.assertEqual(out, "")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")

    def test_one_history_fetch_per_issue(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        queries = []
        self.run_main(lambda q, **v: queries.append(q) or fake(q, **v), "--plan")
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
        mode = os.stat(self.log).st_mode
        with mock.patch.object(router.os, "replace", wraps=os.replace) as rep:
            self.assertEqual(router.main(["--prune", self.log], now=NOW), 0)
        [(src, dst), _] = rep.call_args
        self.assertEqual((os.path.dirname(src), dst), (self.tmp.name, self.log))
        with open(self.log) as f:
            self.assertEqual(f.read(), f"{self.stamp(days=6)} skip: queue empty\nplan: new\n"
                                       f"{self.stamp(hours=1)} start TASK-1 session=b transcript=x\n")
        self.assertEqual(os.stat(self.log).st_mode, mode)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["cfg", "runs.log", "transcripts"])

    def test_unchanged_file_not_rewritten(self):
        self.write([f"{self.stamp(days=1)} skip: queue empty\n"])
        with mock.patch.object(router.os, "replace") as rep:
            router.prune(self.log, NOW)
        rep.assert_not_called()

    def test_missing_log_is_noop(self):
        self.assertEqual(router.main(["--prune", self.log]), 0)
        self.assertFalse(os.path.exists(self.log))


class Usage(unittest.TestCase):
    def test_unknown_flags_rejected(self):
        for argv in (["--help"], ["--plan", "--bogus"], ["--prune"], ["--prune", "x", "--dry-run"], ["--gate", "maybe"],
                     ["--plan", "a", "b"], ["-h"], ["--pick", "--project", "p-dr"], ["--issue", "TASK-1"]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertEqual(router.main(argv, gql=None), 2)
                self.assertIn("usage:", err.getvalue())


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
                self.assertEqual([self.run_main(fake, "--claim")[1].split()[0] for _ in issues], order)
                self.assertEqual({i["state"] for i in fake.issues.values()}, {"In Progress"})

    def test_queue_only_role_accounts_across_projects(self):
        fake = FakeLinear([issue("TASK-1", "Todo"), issue("TASK-2", "Todo", "someone"), issue("TASK-3", "Todo", "pm", project=None),
                           issue("TASK-4", "Todo", "engineer", project=PD, priority=3)])
        self.assertEqual(self.run_main(fake, "--claim")[1], f"TASK-4 https://linear.app/x/TASK-4 {PD}")
        self.assertEqual([i for i in ("TASK-1", "TASK-2", "TASK-3") if fake.issues[i]["state"] != "Todo"], [])
        flt = next(v["f"] for q, v in fake.queries if "issues(filter" in q)
        self.assertEqual((flt["team"], flt["project"], sorted(flt["assignee"]["id"]["in"])),
                         ({"id": {"eq": TEAM}}, {"null": False}, sorted(f"u-{r}" for r in ROLE)))

    def test_claim_only_moves_to_in_progress(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "pm")])
        self.run_main(fake, "--claim")
        (q, v), = fake.mutations
        self.assertNotIn("assignee", q + repr(v))
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("In Progress", who("pm")))

    def test_pick_role_filter(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer", priority=1), issue("TASK-2", "Todo", "researcher", priority=4)])
        self.assertEqual(self.run_main(fake, "--pick", "--role", "researcher")[1], "TASK-2 https://linear.app/x/TASK-2")
        flt = next(v["f"] for q, v in fake.queries if "issues(filter" in q)
        self.assertEqual(flt["assignee"]["id"]["in"], ["u-researcher"])

    def test_pick_unknown_role_exits(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main(FakeLinear([]), "--pick", "--role", "ghost")
        self.assertEqual(cm.exception.code, "no role 'ghost' in roles/")

    def test_board_needs_a_role(self):
        fake = FakeLinear([])
        with self.assertRaises(SystemExit):
            router.Board(fake, [], self.tdir, NOW, True, pipeline.load_config(self.config), only=[])
        self.assertEqual(fake.queries, [])

    def test_capped_todo_goes_to_review(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "researcher", priority=2)])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])
        self.assertEqual((fake.issues["TASK-1"]["assignee"], fake.issues["TASK-1"]["subscribers"]), (who("researcher"), ["me@x.com"]))

    def test_capped_todo_reset_by_user(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        hist = {"TASK-1": [{"createdAt": ago(minutes=200), "actorId": USER, "toStateId": STATES["Todo"]}]}
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")], hist)
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-1 https://linear.app/x/TASK-1 Deep Research")

    def test_claim_skipped_when_no_longer_todo(self):
        class Racy(FakeLinear):
            def __call__(self, query, **v):
                out = super().__call__(query, **v)
                if "issues(filter" in query:
                    for i in self.issues.values():
                        i["state"] = "In Review"
                return out

        fake = Racy([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.run_main(fake, "--claim"), (0, ""))
        self.assertIn("claim: TASK-1 is no longer Todo; skipping", self.err)
        self.assertEqual(fake.mutations, [])

    def test_pick_recovers_then_claims(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))])
        self.assertEqual(self.run_main(fake, "--pick")[1], "TASK-1 https://linear.app/x/TASK-1")
        self.assertEqual(fake.issues["TASK-1"]["comments"][0], router.INTERRUPTED)


class Blockers(Base):
    def test_blocked_skipped_for_next_ready(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1, inverse=[
                               blocker("TASK-7"), blocker("TASK-8", "completed"), blocker("TASK-9", "unstarted")]),
                           issue("TASK-2", "Todo", "researcher", priority=2), issue("TASK-3", "Todo", "researcher", priority=3)])
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-7, TASK-9", "pick: TASK-2 (2 in queue)", "claim: TASK-2 task=deep-research"])
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_done_blockers(self):
        for states, blocked in ((["completed"], False), (["canceled"], False), (["duplicate"], False),
                                (["completed", "started"], True)):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker(f"TASK-{n}", s) for n, s in enumerate(states, 7)])])
            self.run_main(fake, "--claim")
            self.assertEqual(fake.issues["TASK-1"]["state"], "Todo" if blocked else "In Progress", states)
            self.assertEqual([m for m in self.said() if m.startswith("blocked:")], ["blocked: TASK-1 by TASK-8"] if blocked else [], states)

    def test_capped_blocked_stays_todo(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")])])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.run_main(fake, "--claim"), (0, ""))
        self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-7", "pick: queue empty"])
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("Todo", []))
        self.assertNotIn("comments", fake.issues["TASK-1"])

    def test_only_inverse_blocks_block(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=2,
                                 inverse=[blocker("TASK-5", kind="related"), blocker("TASK-6", kind="duplicate")]),
                           issue("TASK-2", "Todo", "researcher", priority=1, inverse=[blocker("TASK-1", "unstarted")])])
        self.assertEqual(self.run_main(fake, "--claim")[1].split()[0], "TASK-1")
        self.assertEqual(self.said(), ["blocked: TASK-2 by TASK-1", "pick: TASK-1 (1 in queue)", "claim: TASK-1 task=deep-research"])
        self.assertFalse(any(re.search(r"\brelations\b", q) for q, _ in fake.queries))

    def test_null_blocker_unreadable(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker(None), blocker("TASK-7", "completed")])])
        self.assertEqual(self.run_main(fake, "--claim"), (0, ""))
        self.assertEqual(self.said(), ["blocked: TASK-1 by (unreadable)", "pick: queue empty"])

    def test_blocker_query_error_reads_per_issue(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1),
                           issue("TASK-2", "Todo", "researcher", priority=2, inverse=[blocker("TASK-7")]),
                           issue("TASK-3", "Todo", "researcher", priority=3, inverse=[blocker("TASK-8", "canceled")])])
        fake.todo_error, fake.unreadable = True, {"TASK-1"}
        self.assertEqual(self.run_main(fake, "--claim"), (0, "TASK-3 https://linear.app/x/TASK-3 Deep Research"))
        said = self.said()
        self.assertRegex(said[0], r"^todo: blocker query failed, reading blockers per issue: linear api error: ")
        self.assertEqual(said[1:], ["blocked: TASK-1 by (unreadable)", "blocked: TASK-2 by TASK-7", "pick: TASK-3 (1 in queue)",
                                    "claim: TASK-3 task=deep-research"])
        todo = fake.reads()
        self.assertEqual(["inverseRelations" in q for q in todo], [True, False])
        self.assertEqual([v["i"] for q, v in fake.queries if "issue(id:" in q and "inverseRelations" in q], ["TASK-1", "TASK-2", "TASK-3"])

    def test_one_todo_read_with_relations(self):
        for argv in (("--claim",), ("--plan",), ("--pick",)):
            fake = FakeLinear([issue("TASK-1", "In Progress", "researcher"), issue("TASK-2", "Todo", "researcher")])
            self.run_main(fake, *argv)
            reads = {s: fake.reads(s) for s in ("Todo", "In Progress")}
            self.assertEqual(len(reads["Todo"]), 1, argv)
            self.assertIn("inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }", reads["Todo"][0], argv)
            self.assertEqual(len(reads["In Progress"]), 0 if argv == ("--claim",) else 1, argv)
            self.assertFalse(any("inverseRelations" in q for q in reads["In Progress"]), argv)
            self.assertFalse(any("issue(id:" in q and "inverseRelations" in q for q, _ in fake.queries), argv)

    def test_dry_run_logs_blocked(self):
        for argv, out, last in ((("--claim", "--dry-run"), "", "pick: TASK-2 (1 in queue)"),
                                (("--plan", "--dry-run"), "new", "plan: new (1 in queue)")):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7"), blocker("TASK-8", "backlog")]),
                               issue("TASK-2", "Todo", "pm", project=PD)])
            self.assertEqual(self.run_main(fake, *argv), (0, out), argv)
            self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-7, TASK-8", last], argv)
            self.assertEqual(fake.mutations, [], argv)


LIST = ["tmux", "list-sessions", "-F", "#{session_name}"]
FULL = ["agent-pm-researcher-TASK-1", "agent-pm-researcher-TASK-2", "agent-pm-pm-TASK-3", "agent-pm-pm-TASK-4", "agent-pm-engineer-TASK-5"]


class FakeShell:
    """sessions: the tmux session names list-sessions prints; server=False: no tmux server, so list-sessions fails."""
    def __init__(self, sessions=(), probe_five=0.2, server=True):
        self.calls, self.five, self.sessions, self.server = [], probe_five, list(sessions), server

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd == LIST:
            if not self.server:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no server running on /tmp/tmux-501/default\n")
            out = "".join(f"{s}\n" for s in self.sessions)
            return subprocess.CompletedProcess(cmd, 0, stdout=out if kw.get("text") else out.encode())
        if cmd[0] == "claude":
            ev = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": self.five}, "seven_day": {"utilization": 0.1}}}}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(ev) + "\n")
        return subprocess.CompletedProcess(cmd, 0)

    def launches(self):
        return [c for c in self.calls if len(c) > 1 and c[1].endswith("launch.py")]


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
        self.assertEqual(router.INTERRUPTED, "The previous run was interrupted. Moving this issue back to the Todo queue.")

    def test_now_skips_hours_and_starts(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, "--now", hour=12)
        (launch,) = self.sh.launches()
        sid = launch[launch.index("--sid") + 1]
        self.assertEqual(launch[0], sys.executable)
        self.assertEqual(launch[2:], ["--issue", "TASK-1", "--url", "https://linear.app/x/TASK-1", "--project", IDS[DR],
                                      "--assignee", ROLE["researcher"], "--sid", sid, "--task", "deep-research", "--mode", "new"])
        self.assertRegex(self.state, rf"start TASK-1 session={sid} transcript={re.escape(pipeline.transcript('TASK-1', sid, self.tdir))}"
                                     r" task=deep-research\n$")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_one_list_sessions_per_tick(self):
        names = ["main", "agent-pm-researcher", "agent-pm-pm-TASK-3", "agent-pm-researcher-TASK-2", "agent-pm-unknown-TASK-4",
                 "agent-pm-researcher-task-5", "agent-pm-researcher-TASK-1"]
        self.assertEqual(router.tmux_sessions(ROLE, FakeShell(names)), [("pm", "TASK-3"), ("researcher", "TASK-1"), ("researcher", "TASK-2")])
        for argv in ((), ("--dry-run",), ("--now", "--issue", "TASK-6")):
            fake = FakeLinear([issue("TASK-6", "Todo", "pm", project=PD)])
            self.tick(fake, *argv, shell=FakeShell(names))
            self.assertEqual([c for c in self.sh.calls if c[0] == "tmux"], [LIST], argv)
            self.assertEqual(self.said()[:2], ["running: pm TASK-3, researcher TASK-1, researcher TASK-2", "full: researcher"], argv)
            self.assertEqual(len(self.sh.launches()), 0 if "--dry-run" in argv else 1, argv)

    def test_failed_list_sessions_is_no_sessions(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, shell=FakeShell(server=False))
        self.assertEqual(self.launched(), "TASK-1")
        self.assertEqual([m for m in self.said() if m.startswith(("running:", "full:", "skip:"))], [])

    def test_capacity(self):
        rows = ((["agent-pm-researcher-TASK-8"], ["running: researcher TASK-8", "plan: new (3 in queue)"], "TASK-1"),
                (["agent-pm-researcher-TASK-8", "agent-pm-researcher-TASK-9"],
                 ["running: researcher TASK-8, researcher TASK-9", "full: researcher", "plan: new (2 in queue)"], "TASK-3"),
                (["agent-pm-engineer-TASK-8"], ["running: engineer TASK-8", "full: engineer", "plan: new (2 in queue)"], "TASK-1"))
        for names, said, launched in rows:
            with self.subTest(names=names):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "pm", priority=3, project=PD),
                                   issue("TASK-3", "Todo", "engineer", priority=2)])
                self.tick(fake, shell=FakeShell(names))
                self.assertEqual(self.said()[:len(said)], said)
                self.assertEqual(self.launched(), launched)

    def test_all_full_skips_before_linear(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        for argv in ((), ("--dry-run",), ("--now", "--issue", "TASK-6")):
            fake = FakeLinear([issue("TASK-6", "Todo", "researcher")])
            self.assertEqual(self.tick(fake, *argv, shell=FakeShell(FULL)), 0)
            self.assertEqual(self.said(), ["running: engineer TASK-5, pm TASK-3, pm TASK-4, researcher TASK-1, researcher TASK-2",
                                           "skip: all roles full (engineer, pm, researcher)"], argv)
            self.assertEqual(fake.queries, [], argv)
            self.assertEqual(self.sh.calls, [LIST], argv)
            self.assertIn("TASK-8", self.state, argv)

    def test_recover_skips_issues_with_a_session(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", priority=1),
                           issue("TASK-2", "In Progress", "researcher", updated=ago(hours=3)),
                           issue("TASK-3", "In Progress", "researcher")], self.hist)
        self.resumable("TASK-1", "a", 60)
        for m in (390, 380, 370, 360):
            self.add("start", "TASK-3", f"c{m}", m)
        self.tick(fake, shell=FakeShell(["agent-pm-researcher-TASK-1", "agent-pm-pm-TASK-2", "agent-pm-engineer-TASK-3"]))
        self.assertEqual(self.said(), ["running: engineer TASK-3, pm TASK-2, researcher TASK-1", "full: engineer",
                                       "plan: nothing to do", "skip: nothing to do"])
        self.assertEqual((fake.mutations, self.sh.launches()), ([], []))
        self.assertEqual([q for q, _ in fake.queries if "history" in q], [])

    def test_full_role_in_progress_recovered_not_resumed(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "engineer", priority=1),
                           issue("TASK-2", "In Progress", "engineer", updated=ago(hours=3)),
                           issue("TASK-3", "In Progress", "engineer"),
                           issue("TASK-4", "In Progress", "researcher", updated=ago(hours=3))], self.hist)
        self.resumable("TASK-1", "a", 60)
        for m in (390, 380, 370, 360):
            self.add("start", "TASK-3", f"c{m}", m)
        self.tick(fake, shell=FakeShell(["agent-pm-engineer-TASK-9"]))
        self.assertEqual({i: fake.issues[i]["state"] for i in ("TASK-1", "TASK-2", "TASK-3")},
                         {"TASK-1": "In Progress", "TASK-2": "Todo", "TASK-3": "In Review"})
        self.assertNotIn("comments", fake.issues["TASK-1"])
        self.assertEqual(fake.issues["TASK-4"]["comments"], [router.INTERRUPTED])
        self.assertEqual(self.launched(), "TASK-4")

    def test_full_role_not_resumed(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "pm", priority=1, project=PD),
                           issue("TASK-2", "In Progress", "researcher", priority=3), issue("TASK-3", "Todo", "engineer", priority=1)], self.hist)
        self.resumable("TASK-1", "a", 120)
        self.resumable("TASK-2", "b", 60)
        self.tick(fake, shell=FakeShell(["agent-pm-pm-TASK-5", "agent-pm-pm-TASK-6", "agent-pm-researcher-TASK-7"]))
        self.assertIn("plan: resume TASK-2 session=b n=1", self.said())
        self.assertEqual(self.launched(), "TASK-2")
        self.assertEqual(self.sh.launches()[0][-4:], ["--mode", "resume", "--k", "1"])
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-3"]["state"], fake.mutations), ("In Progress", "Todo", []))

    def test_full_role_todo_left_alone(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer", priority=1), issue("TASK-2", "Todo", "engineer", priority=1, inverse=[blocker("TASK-7")]),
                           issue("TASK-3", "Todo", "pm", priority=2, project=PD)])
        for m in (390, 380, 370, 360):
            self.add("start", "TASK-1", f"a{m}", m)
        self.tick(fake, shell=FakeShell(["agent-pm-engineer-TASK-9"]))
        self.assertEqual([fake.issues[i]["state"] for i in ("TASK-1", "TASK-2")], ["Todo", "Todo"])
        self.assertNotIn("comments", fake.issues["TASK-1"])
        self.assertEqual([m for m in self.said() if m.startswith("blocked:")], [])
        self.assertEqual(self.launched(), "TASK-3")

    def test_claim_skips_issues_with_a_session(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "researcher", priority=1),
                           issue("TASK-4", "Todo", "researcher", priority=2, created="2026-09-02T00:00:00Z"),
                           issue("TASK-5", "Todo", "pm", priority=2, project=PD, created="2026-09-03T00:00:00Z"),
                           issue("TASK-6", "Todo", "researcher", priority=2, created="2026-09-01T00:00:00Z")])
        names = ["agent-pm-researcher-TASK-1", "agent-pm-pm-TASK-2"]
        for ident in ("TASK-5", "TASK-6", "TASK-4"):
            self.tick(fake, shell=FakeShell(names))
            self.assertEqual(self.launched(), ident)
        self.assertEqual(self.said()[:4], ["running: pm TASK-2, researcher TASK-1", "pick: TASK-1 has a running session; skipping",
                                           "pick: TASK-2 has a running session; skipping", "plan: new (1 in queue)"])
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-2"]["state"]), ("Todo", "Todo"))

    def test_nothing_to_do_with_full_role(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer")])
        self.tick(fake, shell=FakeShell(["agent-pm-engineer-TASK-9"]))
        self.assertEqual(self.said(), ["running: engineer TASK-9", "full: engineer", "plan: nothing to do", "skip: nothing to do"])
        self.assertEqual(self.sh.calls, [LIST])
        self.assertEqual(sorted(v["e"] for q, v in fake.queries if "users(filter" in q), sorted(ROLE.values()))
        self.assertEqual(fake.mutations, [])

    def test_issue_flag_with_a_session_skips(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        for argv in (("--now", "--issue", "TASK-1"), ("--now", "--issue", "TASK-1", "--dry-run")):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
            self.assertEqual(self.tick(fake, *argv, shell=FakeShell(["agent-pm-pm-TASK-1"])), 0)
            self.assertEqual(self.said(), ["running: pm TASK-1", "skip: TASK-1 has a running session"], argv)
            self.assertEqual((fake.queries, self.sh.calls), ([], [LIST]), argv)
            self.assertIn("TASK-8", self.state, argv)

    def test_issue_flag_of_full_role_skips(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        for argv in (("--now", "--issue", "TASK-1"), ("--now", "--issue", "TASK-1", "--dry-run")):
            fake = FakeLinear([issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "Todo", "researcher")])
            self.assertEqual(self.tick(fake, *argv, shell=FakeShell(["agent-pm-engineer-TASK-9"])), 0)
            self.assertEqual(self.said(), ["running: engineer TASK-9", "full: engineer", "skip: TASK-1 belongs to full role engineer"], argv)
            self.assertEqual([v for _, v in fake.queries], [{"f": {"team": {"id": {"eq": TEAM}}, "id": {"eq": "TASK-1"}}}], argv)
            self.assertEqual(fake.mutations, [], argv)
            self.assertEqual(self.sh.calls, [LIST], argv)
            self.assertIn("TASK-8", self.state, argv)

    def test_issue_flag_reads_no_assignee_when_no_role_is_full(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "researcher", priority=4)])
        self.tick(fake, "--now", "--issue", "TASK-2", shell=FakeShell(["agent-pm-researcher-TASK-8"]))
        self.assertEqual(self.launched(), "TASK-2")
        self.assertFalse(any("id" in v["f"] for q, v in fake.queries if "issues(filter" in q))

    def test_issue_flag_of_role_not_full_while_another_full(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer", priority=1), issue("TASK-2", "Todo", "researcher", priority=4)])
        self.tick(fake, "--now", "--issue", "TASK-2", shell=FakeShell(["agent-pm-engineer-TASK-9", "agent-pm-researcher-TASK-8"]))
        self.assertEqual(self.launched(), "TASK-2")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-2"]["state"]), ("Todo", "In Progress"))

    def test_issue_flag_not_a_role_issue_while_full(self):
        for ident in ("TASK-9", "TASK-2", "TASK-3"):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher"), issue("TASK-2", "Todo", "someone"), issue("TASK-3", "Todo")])
            self.tick(fake, "--now", "--issue", ident, shell=FakeShell(["agent-pm-engineer-TASK-8"]))
            self.assertIn(f"pick: {ident} is not a Todo issue assigned to a role account", self.err)
            self.assertEqual((self.sh.launches(), fake.mutations), ([], []))

    def test_dry_run(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "Todo", "researcher")])
        self.tick(fake, "--dry-run", hour=12, shell=FakeShell(["agent-pm-pm-TASK-5", "agent-pm-pm-TASK-6", "agent-pm-engineer-TASK-7"]))
        said = self.said()
        self.assertEqual(said[:5], ["skip: outside hours", "running: engineer TASK-7, pm TASK-5, pm TASK-6", "full: engineer, pm",
                                    "plan: new (1 in queue)", "plan: new"])
        self.assertRegex(said[5], r"^usage: status=allowed .*\(any role\)$")
        self.assertEqual(len(said), 6)
        self.assertEqual([c[0] for c in self.sh.calls].count("claude"), 1)
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
        self.assertEqual(self.said()[3:5], ["plan: nothing to do", "plan: nothing"])
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
                self.assertEqual(self.said(), ["plan: resume TASK-2 session=a n=1", "blocked: TASK-1 by TASK-7"] if resume
                                 else ["blocked: TASK-1 by TASK-7", "plan: new (1 in queue)"], (argv, resume))
                self.assertEqual(([c[0] for c in self.sh.calls].count("claude"), self.sh.launches(), fake.mutations), (0, [], []), (argv, resume))
                self.assertEqual(self.state, before, (argv, resume))
                self.assertEqual(len(fake.reads()), 1, (argv, resume))

    def test_ready_tick_reads_todo_once(self):
        for argv in ((), ("--now", "--issue", "TASK-2")):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1, inverse=[blocker("TASK-7")]),
                               issue("TASK-2", "Todo", "researcher", priority=2), issue("TASK-3", "Todo", "pm", priority=3, project=PD)])
            self.assertEqual(self.tick(fake, *argv), 0, argv)
            self.assertEqual(self.launched(), "TASK-2", argv)
            self.assertEqual(len(fake.reads()), 1, argv)
            self.assertIn("plan: new (2 in queue)", self.err, argv)
            self.assertEqual(self.said().count("blocked: TASK-1 by TASK-7"), 1, argv)

    def test_issue_flag_after_resume_reads_todo_once(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher"), issue("TASK-2", "Todo", "researcher")], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.tick(fake, "--now", "--issue", "TASK-2")
        self.assertEqual(self.launched(), "TASK-2")
        self.assertEqual(len(fake.reads()), 1)

    def test_usage_blocked(self):
        for five, resume in ((0.9, False), (0.95, False), (0.9, True)):
            with self.subTest(five=five, resume=resume):
                self.lines, self.hist = [], {}
                issues = [issue("TASK-1", "Todo", "researcher")]
                if resume:
                    issues.append(issue("TASK-2", "In Progress", "pm", project=PD))
                    self.resumable("TASK-2", "b", 60)
                fake = FakeLinear(issues, self.hist)
                before = self.unmap("\n".join(self.lines) + ("\n" if self.lines else ""))
                self.tick(fake, shell=FakeShell(probe_five=five))
                self.assertEqual(self.said()[-1], f"skip: {'resume' if resume else 'new'} blocked by usage: "
                                                  f"status=allowed five_hour={five} seven_day=0.1")
                self.assertEqual((self.sh.launches(), fake.mutations, self.state), ([], [], before))

    def test_usage_tier_one_launches_any_role_not_full(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "pm", priority=3, project=PD)])
        self.tick(fake, shell=FakeShell(["agent-pm-researcher-TASK-8"], probe_five=0.5))
        self.assertEqual(self.said(), ["running: researcher TASK-8", "plan: new (2 in queue)", "pick: TASK-1 (2 in queue)",
                                       "claim: TASK-1 task=deep-research", f"launch TASK-1 ({DR}) exit=0"])

    def test_usage_tier_two_claims_for_a_role_with_no_session(self):
        for five in (0.6, 0.7, 0.89):
            with self.subTest(five=five):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "pm", priority=3, project=PD)])
                self.tick(fake, shell=FakeShell(["agent-pm-researcher-TASK-8"], probe_five=five))
                self.assertEqual(self.said(), ["running: researcher TASK-8", "plan: new (2 in queue)",
                                               f"usage: status=allowed five_hour={five} seven_day=0.1 (only roles with no session: engineer, pm)",
                                               "pick: TASK-2 (1 in queue)", "claim: TASK-2 task=product-design", f"launch TASK-2 ({PD}) exit=0"])
                self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-2"]["state"]), ("Todo", "In Progress"))

    def test_usage_tiers_resume(self):
        """researcher (one session) has the first resume candidate; pm (no session) a later candidate or a Todo issue."""
        resume, new = ["--mode", "resume", "--k", "1"], ["--mode", "new"]
        rows = ((0.5, False, "resume", "TASK-1", resume), (0.7, False, "resume", "TASK-2", resume), (0.7, True, "start", "TASK-3", new))
        for five, todo, line, launched, mode in rows:
            with self.subTest(five=five, todo=todo):
                self.lines, self.hist = [], {}
                issues = [issue("TASK-1", "In Progress", "researcher", priority=1)]
                self.resumable("TASK-1", "a", 120)
                if todo:
                    issues.append(issue("TASK-3", "Todo", "pm", priority=4, project=PD))
                else:
                    issues.append(issue("TASK-2", "In Progress", "pm", priority=3, project=PD))
                    self.resumable("TASK-2", "b", 60)
                fake = FakeLinear(issues, self.hist)
                self.tick(fake, shell=FakeShell(["agent-pm-researcher-TASK-8"], probe_five=five))
                self.assertIn("plan: resume TASK-1 session=a n=1", self.said())
                self.assertEqual(self.launched(), launched)
                (launch,) = self.sh.launches()
                self.assertEqual(launch[launch.index("--mode"):], mode)
                self.assertEqual(self.state.splitlines()[-1].split()[2:4], [line, launched])
                self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_usage_tier_two_skips_when_only_busy_roles_have_work(self):
        nothing = "skip: nothing for an eligible role (status=allowed five_hour=0.7 seven_day=0.1)"
        rows = ((["agent-pm-researcher-TASK-8"], "engineer, pm"),
                (["agent-pm-researcher-TASK-8", "agent-pm-pm-TASK-9", "agent-pm-engineer-TASK-7"], "none"))
        for names, idle in rows:
            for resume in (False, True):
                with self.subTest(idle=idle, resume=resume):
                    self.lines, self.hist = [], {}
                    issues = [issue("TASK-1", "Todo", "researcher")]
                    if resume:
                        issues.append(issue("TASK-2", "In Progress", "researcher"))
                        self.resumable("TASK-2", "b", 60)
                    fake = FakeLinear(issues, self.hist)
                    before = self.unmap("\n".join(self.lines) + ("\n" if self.lines else ""))
                    self.tick(fake, shell=FakeShell(names, probe_five=0.7))
                    self.assertEqual(self.said()[-2:], [f"usage: status=allowed five_hour=0.7 seven_day=0.1 (only roles with no session: {idle})",
                                                        nothing])
                    self.assertEqual((self.sh.launches(), fake.mutations, self.state), ([], [], before))

    def test_usage_tier_two_issue_flag(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "pm", priority=4, project=PD),
                           issue("TASK-3", "In Progress", "pm", priority=1, project=PD)], self.hist)
        self.resumable("TASK-3", "c", 60)
        self.tick(fake, "--now", "--issue", "TASK-1", shell=FakeShell(["agent-pm-researcher-TASK-8"], probe_five=0.7))
        self.assertEqual(self.said()[-1], "skip: nothing for an eligible role (status=allowed five_hour=0.7 seven_day=0.1)")
        self.assertEqual((self.sh.launches(), fake.mutations), ([], []))
        self.tick(fake, "--now", "--issue", "TASK-2", shell=FakeShell(["agent-pm-researcher-TASK-8"], probe_five=0.7))
        self.assertEqual(self.launched(), "TASK-2")
        self.assertEqual(self.sh.launches()[0][-2:], ["--mode", "new"])
        self.assertEqual([fake.issues[i]["state"] for i in ("TASK-1", "TASK-2", "TASK-3")], ["Todo", "In Progress", "In Progress"])

    def test_dry_run_usage_tiers(self):
        for five, said in ((0.5, "any role"), (0.6, "roles with no session"), (0.9, "blocked")):
            with self.subTest(five=five):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.tick(fake, "--dry-run", shell=FakeShell(["agent-pm-researcher-TASK-8"], probe_five=five))
                self.assertEqual(self.said()[-2:], ["plan: new", f"usage: status=allowed five_hour={five} seven_day=0.1 ({said})"])
                self.assertEqual((self.sh.launches(), fake.mutations), ([], []))

    def test_resume(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.tick(fake)
        (launch,) = self.sh.launches()
        self.assertEqual(launch[2:10], ["--issue", "TASK-1", "--url", "https://linear.app/x/TASK-1", "--project", IDS[DR],
                                        "--assignee", ROLE["researcher"]])
        self.assertEqual(launch[-4:], ["--mode", "resume", "--k", "1"])
        self.assertIn("--sid", launch)
        self.assertTrue(self.state.endswith("resume TASK-1 session=a n=1 task=deep-research\n"))

    def test_issue_flag_still_recovers(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)), issue("TASK-2", "Todo", "researcher")])
        self.tick(fake, "--now", "--issue", "TASK-1")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.INTERRUPTED])
        (launch,) = self.sh.launches()
        self.assertEqual(launch[launch.index("--issue") + 1], "TASK-1")

    def test_issue_flag_claims_that_issue(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1), issue("TASK-2", "Todo", "researcher", priority=4)])
        self.tick(fake, "--now", "--issue", "TASK-2")
        (launch,) = self.sh.launches()
        self.assertEqual(launch[launch.index("--issue") + 1], "TASK-2")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")
        self.assertFalse(any("id" in v["f"] for q, v in fake.queries if "issues(filter" in q))

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


class TaskLabels(Base):
    """A copy of the repo's roles/ and tasks/ (which ship light-research) plus orphan, no role's task; deliberately depends on the shipped researcher config."""
    ORPHAN = ('Task label "Orphan" is not one of researcher\'s tasks (deep-research, light-research). '
              "Fix the label or the assignee, then move the issue back to Todo.")

    def setUp(self):
        super().setUp()
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG + TASK_LABELS)
        self.root = os.path.join(self.tmp.name, "root")
        for d in ("roles", "tasks"):
            shutil.copytree(os.path.join(pipeline.ROOT, d), os.path.join(self.root, d))
        for ext, text in ((".md", "x\n"), (".toml", 'model = "sonnet"\neffort = "low"\n')):
            with open(os.path.join(self.root, "tasks", "orphan" + ext), "w") as f:
                f.write(text)

    def test_task_for(self):
        outside = [label("Urgent", STRAY, None), label("Light Research", LIGHT, "00000000-0000-4000-8000-000000000003")]
        role_tasks = ["deep-research", "light-research"]
        label_tasks = {LIGHT: "light-research", DEEP: "deep-research", ORPHAN_LABEL: "orphan"}
        fix = "Fix the label or the assignee, then move the issue back to Todo."
        absent = "is not in pipeline.toml's [task_labels]. Fix the label, then move the issue back to Todo."
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
        self.assertEqual([(op, v["i"]) for op, (_, v) in zip(ops(fake), fake.mutations)],
                         [("issueSubscribe", "TASK-1"), ("commentCreate", "TASK-1"), ("issueUpdate", "TASK-1"),
                          ("issueUpdate", "TASK-2")])
        self.assertEqual(fake.mutations[2][1]["u"], {"stateId": STATES["In Review"]})
        self.assertIn("claim: TASK-1 bad task label; In Review", self.said())
        self.assertEqual(self.launched(), "TASK-2")
        self.assertEqual([line.split()[2:4] for line in self.state.splitlines()], [["start", "TASK-2"]])
        self.assertTrue(self.state.endswith(" task=deep-research\n"))

    def test_blocked_invalid_label_waits_for_its_blocker(self):
        for argv, skip in (((), ["skip: nothing to do"]), (("--now", "--issue", "TASK-1"), [])):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", labels=[label("Orphan", ORPHAN_LABEL)], inverse=[blocker("TASK-7")])])
            self.tick(fake, *argv)
            self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-7", "plan: nothing to do"] + skip, argv)
            self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations, self.sh.launches()), ("Todo", [], []), argv)
            self.assertNotIn(RECHECK, [q for q, _ in fake.queries], argv)
            fake.issues["TASK-1"]["inverseRelations"]["nodes"][0]["issue"]["state"]["type"] = "completed"
            self.tick(fake, *argv)
            t = fake.issues["TASK-1"]
            self.assertEqual((t["state"], t["comments"]), ("In Review", [self.ORPHAN]), argv)
            self.assertEqual(self.said(), ["plan: new (1 in queue)", "pick: TASK-1 (1 in queue)", "claim: TASK-1 bad task label; In Review",
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
        for argv in (("--claim", "--dry-run"), ("--pick", "--dry-run")):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", labels=[label("Orphan", ORPHAN_LABEL)])])
            self.assertEqual(self.run_main(fake, *argv), (0, ""), argv)
            self.assertEqual(self.said(), ["pick: TASK-1 (1 in queue)"], argv)
            self.assertEqual(([q for q, _ in fake.queries if q == RECHECK], fake.mutations), ([], []), argv)

    def test_nfr1_claim_reads_one_query_per_issue(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", labels=[label("Light Research", LIGHT)])])
        self.tick(fake)
        self.assertEqual([(q, v) for q, v in fake.queries if "issue(id:" in q], [(RECHECK, {"i": "TASK-1"})])
        todo = [q for q, _ in fake.queries].index(fake.reads()[0])
        self.assertEqual([q for q, _ in fake.queries[todo + 1:]], [RECHECK, fake.mutations[0][0]])

    def test_fr12a_manual_claim_takes_default_task_only(self):
        for capped in (False, True):
            if capped:
                for sid in "abcd":
                    self.add("start", "TASK-3", sid, 300)
            for argv in (("--pick",), ("--pick", "--role", "researcher"), ("--claim",)):
                with self.subTest(capped=capped, argv=argv):
                    fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1, labels=[label("Light Research", LIGHT)]),
                                       issue("TASK-2", "Todo", "researcher", priority=2, labels=[label("Orphan", ORPHAN_LABEL)]),
                                       issue("TASK-3", "Todo", "researcher", priority=3, labels=[label("Deep Research", DEEP)])])
                    out = "" if capped else "TASK-3 https://linear.app/x/TASK-3" + (" Deep Research" if argv == ("--claim",) else "")
                    self.assertEqual(self.run_main(fake, *argv), (0, out))
                    last = (["pick: TASK-3 reached 4 attempts; In Review", "pick: nothing claimable"] if capped
                            else ["pick: TASK-3 (3 in queue)", "claim: TASK-3 task=deep-research"])
                    self.assertEqual(self.said(), ["pick: TASK-1 (3 in queue)", "claim: TASK-1 task=light-research is not researcher's default; skipping",
                                                   "pick: TASK-2 (3 in queue)", "claim: TASK-2 bad task label; In Review"] + last)
                    self.assertEqual([fake.issues[i]["state"] for i in ("TASK-1", "TASK-2", "TASK-3")],
                                     ["Todo", "In Review", "In Review" if capped else "In Progress"])
                    self.assertNotIn("TASK-1", [v["i"] for _, v in fake.mutations])

    def test_fr1_tick_checks_the_task_group_once_and_stops_on_a_bad_one(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake)
        self.assertEqual([v for q, v in fake.queries if q == pipeline.Q_TASK_GROUP], [{"i": TASK_GROUP}])
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        fake.group = {"isGroup": False}
        with self.assertRaises(SystemExit) as cm:
            self.tick(fake)
        self.assertEqual(cm.exception.code, f"pipeline.toml: task_label_group {TASK_GROUP} is not a label group")
        self.assertEqual((fake.queries, fake.mutations), ([(pipeline.Q_TEAM, {"t": TEAM}), (pipeline.Q_TASK_GROUP, {"i": TASK_GROUP})], []))
        self.assertEqual(self.sh.calls, [LIST])

    def test_fr9_fr13_resume_passes_the_recorded_task(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")], self.hist)
        self.moved("TASK-1", 61)
        self.add("resume", "TASK-1", "a", 60, "light-research")
        self.add("resume", "TASK-1", "a", 50)
        self.touch("TASK-1", "a", 40)
        self.tick(fake)
        (launch,) = self.sh.launches()
        self.assertEqual(launch[launch.index("--sid"):], ["--sid", self.sid("a"), "--task", "light-research", "--mode", "resume", "--k", "3"])
        self.assertTrue(self.state.endswith(" resume TASK-1 session=a n=3 task=light-research\n"))
        self.assertIn("plan: resume TASK-1 session=a n=3", self.said())

    def test_fr14_no_record_resumes_the_assignee_roles_default(self):
        for role, project, task in (("researcher", DR, "deep-research"), ("pm", PD, "product-design")):
            self.lines, self.hist = [], {}
            fake = FakeLinear([issue("TASK-1", "In Progress", role, project=project, labels=[label("Light Research", LIGHT)])], self.hist)
            self.resumable("TASK-1", "a", 60)
            self.tick(fake)
            (launch,) = self.sh.launches()
            self.assertEqual(launch[-6:], ["--task", task, "--mode", "resume", "--k", "1"], role)
            self.assertTrue(self.state.endswith(f" resume TASK-1 session=a n=1 task={task}\n"), role)

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
                             ("In Review", who(role), [f'The interrupted run\'s task "{task}" is not one of {role}\'s tasks ({tasks}); needs a look.'],
                              ["me@x.com"]), role)
            self.assertEqual(ops(fake), ["issueSubscribe", "commentCreate", "issueUpdate"], role)
            self.assertEqual(fake.mutations[2][1]["u"], {"stateId": STATES["In Review"]}, role)
            self.assertEqual(self.said(), [f"recover: TASK-1 task={task} is not one of {role}'s tasks; In Review",
                                           "plan: nothing to do", "skip: nothing to do"], role)
            self.assertEqual((self.sh.launches(), self.state), ([], before), role)

    def test_fr15_every_resumable_issue_is_checked(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", priority=1),
                           issue("TASK-2", "In Progress", "researcher", priority=2)], self.hist)
        self.resumable("TASK-1", "a", 60, "light-research")
        self.resumable("TASK-2", "b", 60, "orphan")
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 a 1 https://linear.app/x/TASK-1 Deep Research")
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
