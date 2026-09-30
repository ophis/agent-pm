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
from board_ids import HEADER, STATES as IDS_BY_KEY, TEAM, team_node  # noqa: E402
import pipeline  # noqa: E402
import router  # noqa: E402

NOW = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
AGENT, USER = "agent", "user"   # history actor ids: an agent account, a human_members user
STATES = {"Todo": IDS_BY_KEY["todo"], "In Progress": IDS_BY_KEY["in_progress"], "In Review": IDS_BY_KEY["in_review"]}
NAMES = {i: n for n, i in STATES.items()}
DR, PD = "Deep Research", "Product Design"
IDS = {DR: "p-dr", PD: "p-pd", "Engineering": "p-eng"}
ROLE = {name: r.account for name, r in pipeline.registry()[0].items()}  # the repo's roles/
CONFIG = HEADER + '[roles.researcher]\nnext = "pm"\n[roles.pm]\nnext = "engineer"\n[roles.engineer]\n'


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
        self.state_ids = None
        self.missing = set()
        self.todo_error = False
        self.unreadable = set()

    def __call__(self, query, **v):
        self.queries.append((query, v))
        if query == pipeline.Q_TEAM:
            return {"teams": {"nodes": [team_node(self.state_ids)]}}
        if "users(filter" in query:
            users = {"me@x.com": USER, "b@x.com": "user-b", **{a.lower(): f"u-{r}" for r, a in ROLE.items()}}
            return {"users": {"nodes": [{"id": u} for e, u in users.items() if v["e"].lower() == e and e not in self.missing]}}
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
            drop = {"state"} if "inverseRelations" in query else {"state", "inverseRelations"}
            return {"issues": {"nodes": [{k: x for k, x in i.items() if k not in drop} for i in self.issues.values()
                                         if f["team"]["id"]["eq"] == TEAM and f["project"] == {"null": False} and i["project"]
                                         and i["assignee"] and i["assignee"]["id"] in f["assignee"]["id"]["in"]
                                         and i["state"] == NAMES[f["state"]["id"]["eq"]]]}}
        if "issue(id:" in query and "inverseRelations" in query:
            if v["i"] in self.unreadable:
                raise SystemExit("linear api error: [{'message': 'Entity not found'}]")
            return {"issue": {"inverseRelations": self.issues[v["i"]]["inverseRelations"]}}
        if "state { id }" in query:
            return {"issue": {"state": {"id": STATES[self.issues[v["i"]]["state"]]}}}
        raise AssertionError(query)

    def reads(self, state="Todo"):
        return [q for q, v in self.queries if "issues(filter" in q and v["f"].get("state") == {"id": {"eq": STATES[state]}}]


def ops(fake):
    return [re.search(r"\{ (\w+)\(", q).group(1) for q, _ in fake.mutations]


def issue(ident, state, role=None, updated=None, priority=0, created="2026-09-01T00:00:00Z", project=DR, inverse=()):
    # id == identifier so mutations and history can be keyed by either
    return {"id": ident, "identifier": ident, "url": f"https://linear.app/x/{ident}", "state": state,
            "project": project and {"id": IDS[project], "name": project},
            "assignee": who(role), "priority": priority, "createdAt": created, "updatedAt": updated or ago(minutes=5),
            "inverseRelations": {"nodes": list(inverse)}}


def blocker(ident, state="started", kind="blocks"):
    """An inverse relation node: ident (None = unreadable) relates to the issue by kind; state is ident's state type."""
    return {"type": kind, "issue": ident and {"identifier": ident, "state": {"type": state}}}


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

    def resumable(self, ident, sid, minutes_ago):
        """A start line for sid, an old <sid>.jsonl, and a move to In Progress just before the start."""
        self.moved(ident, minutes_ago + 1)
        self.add("start", ident, sid, minutes_ago)
        self.touch(ident, sid, 40)

    def add(self, kind, ident, sid, minutes_ago):
        ts = (NOW - timedelta(minutes=minutes_ago)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        s = self.sid(sid)
        extra = "n=1" if kind == "resume" else f"transcript={pipeline.transcript(ident, s, self.tdir)}"
        self.lines.append(f"{ts} {kind} {ident} session={s} {extra}")

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
            rc = router.main(list(argv) + [self.log], gql=fake, now=NOW, tdir=self.tdir, config=self.config)
        self.err = self.unmap(err.getvalue())
        return rc, self.unmap(out.getvalue().strip())

    def said(self):
        """The logged messages, without timestamps."""
        return [line.split(" ", 2)[2] for line in self.err.splitlines()]


class ParseAndLiveness(Base):
    def test_parse_ignores_other_lines(self):
        self.add("start", "TASK-1", "a", 60)
        self.add("resume", "TASK-1", "a", 30)
        self.lines += ["pick: TASK-1 (3 in queue)", "2026-09-26 13:04:05 start --dry-run log=/x.jsonl",
                       "2026-09-26 20:45:59 skip: queue empty", "recover: TASK-2 (last updated x)",
                       "2026-09-26 23:00:00 end TASK-1 session=a exit=1"]
        with open(self.log, "w") as f:
            f.write("\n".join(self.lines) + "\n")
        entries = router.parse_log(self.log)
        self.assertEqual([(e[1], e[2], e[3]) for e in entries], [("start", "TASK-1", self.sid("a")), ("resume", "TASK-1", self.sid("a"))])
        self.assertAlmostEqual(entries[0][0].timestamp(), (NOW - timedelta(minutes=60)).timestamp())

    def test_missing_log_is_empty(self):
        self.assertEqual(router.parse_log(os.path.join(self.tmp.name, "nope")), [])

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

    def test_other_folders_ignored(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
        self.add("start", "TASK-1", "a", 40)
        self.touch("TASK-2", "a", 5)
        legacy = os.path.join(self.tdir, pipeline.escape(pipeline.WORK), self.sid("a") + ".jsonl")
        os.makedirs(os.path.dirname(legacy))
        open(legacy, "w").close()
        self.assertFalse(router.is_live(self.tdir, "TASK-1", self.sid("a"), NOW))
        self.assertEqual(self.run_main(fake, "--plan")[1], "new")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")
        self.assertIn("recover: TASK-1 session=a has no transcript", self.err)


def event(status="allowed", five=0.1, **week):
    windows = {"five_hour": {"utilization": five, "resetsAt": 1}}
    windows.update({k: {"utilization": v, "resetsAt": 2} for k, v in week.items()})
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "utilization": five, "rateLimitType": "five_hour", "resetsAt": 1, "unifiedWindows": windows}})


class Gate(unittest.TestCase):
    def ok(self, kind, *lines):
        return router.gate(kind, list(lines))[0]

    def test_no_event(self):
        self.assertEqual(router.gate("new", ['{"type":"system"}', "garbage"]), (False, "no rate_limit_event"))

    def test_new(self):
        self.assertTrue(self.ok("new", event(five=0.89)))
        self.assertFalse(self.ok("new", event(five=0.9)))
        self.assertFalse(self.ok("new", event(status="rejected", five=0.1)))
        self.assertFalse(self.ok("new", event(five=0.1, seven_day=1.0)))

    def test_resume(self):
        self.assertTrue(self.ok("resume", event(five=0.89, seven_day=0.99, seven_day_opus=0.5)))
        self.assertFalse(self.ok("resume", event(five=0.9)))
        self.assertFalse(self.ok("resume", event(status="rejected", five=0.1)))
        self.assertFalse(self.ok("resume", event(five=0.1, seven_day=0.2, seven_day_opus=1.0)))
        self.assertTrue(self.ok("resume", event(status="allowed_warning", five=0.5)))

    def test_last_event_wins(self):
        self.assertFalse(self.ok("new", event(five=0.1), event(five=0.9)))
        self.assertTrue(self.ok("new", event(five=0.9), "{}", event(five=0.1)))

    def test_main_reads_stdin(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = router.main(["--gate", "resume"], gql=None, stdin=io.StringIO(event(five=0.5, seven_day=0.3) + "\n"))
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), "status=allowed five_hour=0.5 seven_day=0.3")


class Plan(Base):
    def test_nothing(self):
        fake = FakeLinear([issue("TASK-1", "In Review", "researcher")])
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))

    def test_new(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.run_main(fake, "--plan")[1], "new")
        self.assertEqual(fake.mutations, [])

    def test_resume_has_no_session_cap(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=5)), issue("TASK-2", "Todo", "researcher"))
        self.resumable("TASK-1", "sid1", 300)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 sid1 1 https://linear.app/x/TASK-1 Deep Research")
        self.add("resume", "TASK-1", "sid1", 200)
        self.add("resume", "TASK-1", "sid1", 100)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 sid1 3 https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(fake.mutations, [], "a candidate is never moved, even when stale")

    def test_attempt_cap_beats_resume(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=5)))
        self.resumable("TASK-1", "sid1", 300)
        for m in (200, 150, 100):
            self.add("resume", "TASK-1", "sid1", m)
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])

    def test_live_session_not_resumed_or_recovered(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=5)))
        self.add("start", "TASK-1", "sid1", 300)
        self.touch("TASK-1", "sid1", 5, "subagents/workflows/r/journal.jsonl")
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.mutations, [])

    def test_original_bug_live_issue_then_new_issue(self):
        a = issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))
        fake = self.fake(a, issue("TASK-2", "Todo", "researcher"))
        self.resumable("TASK-1", "a", 200)
        self.touch("TASK-1", "a", 5)
        self.assertEqual(self.run_main(fake, "--plan")[1], "new")
        self.assertEqual(fake.mutations, [])
        fake.issues["TASK-2"].update(state="In Progress", assignee=who("researcher"))
        self.resumable("TASK-2", "b", 60)
        self.touch("TASK-1", "a", 40)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 a 1 https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(fake.mutations, [])

    def order(self, *specs):
        """specs: (ident, priority, start minutes ago); returns the resumed issue."""
        fake = self.fake(*[issue(i, "In Progress", "researcher", priority=p) for i, p, _ in specs])
        for ident, _, m in specs:
            self.resumable(ident, ident.lower(), m)
        return self.run_main(fake, "--plan")[1].split()[1]

    def test_priority_beats_age(self):
        self.assertEqual(self.order(("TASK-1", 3, 300), ("TASK-2", 2, 100)), "TASK-2")

    def test_equal_priority_oldest_first_line(self):
        self.assertEqual(self.order(("TASK-1", 2, 100), ("TASK-2", 2, 300)), "TASK-2")

    def test_no_priority_ranks_lowest(self):
        self.assertEqual(self.order(("TASK-1", 0, 300), ("TASK-2", 4, 100)), "TASK-2")

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

    def assert_walk(self, fake):
        states = {i: fake.issues[i]["state"] for i in ("TASK-1", "TASK-3", "TASK-4", "TASK-5", "TASK-6")}
        self.assertEqual(states, {"TASK-1": "Todo", "TASK-3": "In Progress", "TASK-4": "In Review",
                                  "TASK-5": "In Review", "TASK-6": "In Progress"})
        self.assertEqual(fake.issues["TASK-1"]["comments"][0], router.INTERRUPTED)
        self.assertNotIn("comments", fake.issues["TASK-3"])

    def test_walk_moves_before_and_after_candidate(self):
        fake = self.walk_fixture()
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-3 c 1 https://linear.app/x/TASK-3 Deep Research")
        self.assert_walk(fake)

    def test_dry_run_walk(self):
        fake = self.walk_fixture()
        self.assertEqual(self.run_main(fake, "--plan", "--dry-run")[1], "resume TASK-3 c 1 https://linear.app/x/TASK-3 Deep Research")
        self.assertEqual(fake.mutations, [])
        self.assertIn("recover: TASK-1 session=a has no transcript", self.err)
        self.assertIn("recover: TASK-5 reached 4 attempts", self.err)

    def test_no_mode_walks_then_claims(self):
        fake = self.walk_fixture()
        self.assertEqual(self.run_main(fake, "--pick")[1], "TASK-9 https://linear.app/x/TASK-9")
        self.assert_walk(fake)

    def test_live_issue_skipped_for_next(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", priority=1), issue("TASK-3", "In Progress", "researcher", priority=3))
        self.resumable("TASK-1", "a", 300)
        self.touch("TASK-1", "a", 5)
        self.resumable("TASK-3", "c", 100)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-3 c 1 https://linear.app/x/TASK-3 Deep Research")
        self.assertEqual(fake.mutations, [])

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
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)),
                           issue("TASK-2", "In Progress", "researcher", updated=ago(hours=1))])
        self.run_main(fake, "--plan")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")
        self.assertEqual(fake.issues["TASK-1"]["comments"][0], router.INTERRUPTED)
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

    def test_not_ours_not_candidate(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "someone", updated=ago(hours=5))])
        self.add("start", "TASK-1", "sid1", 300)
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.mutations, [])

    def test_recover_requeue_keeps_assignee(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "engineer", updated=ago(hours=3), project=PD)])
        self.run_main(fake, "--plan")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("Todo", who("engineer")))
        self.assertFalse(any("assigneeId" in repr(v) for _, v in fake.mutations))

    def test_recover_skips_other_assignees(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "someone", updated=ago(hours=3)),
                           issue("TASK-2", "In Progress", None, updated=ago(hours=3))])
        self.run_main(fake, "--plan")
        self.assertEqual(fake.mutations, [])

    def test_attempt_cap_moves_to_review(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=40))])
        for i, sid in enumerate(["a", "b", "c"]):
            self.add("start", "TASK-1", sid, 400 - i * 50)
        self.add("resume", "TASK-1", "c", 200)
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])
        self.assertNotIn("subscribers", fake.issues["TASK-1"])

    def test_attempt_cap_subscribes_humans(self):
        self.config = self.write_config('human_members = ["me@x.com", "b@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=40))])
        for i, sid in enumerate(["a", "b", "c", "d"]):
            self.add("start", "TASK-1", sid, 400 - i * 50)
        self.run_main(fake, "--plan")
        t = fake.issues["TASK-1"]
        self.assertEqual((t["state"], t["assignee"], t["subscribers"]), ("In Review", who("researcher"), ["me@x.com", "b@x.com"]))
        self.assertEqual(ops(fake), ["issueSubscribe", "issueSubscribe", "commentCreate", "issueUpdate"])
        self.assertEqual(fake.mutations[-1][1]["u"], {"stateId": STATES["In Review"]})

    def test_interrupted_keeps_assignee_and_subscribes_no_one(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=5))])
        self.run_main(fake, "--plan")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("Todo", who("researcher")))
        self.assertNotIn("subscribers", fake.issues["TASK-1"])

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

    def test_attempts_after_prune(self):
        self.write([f"{self.stamp(days=8, minutes=m)} start TASK-1 session=s{m} transcript=x\n" for m in (4, 3, 2)]
                   + [f"{self.stamp(days=1)} start TASK-1 session=s1 transcript=x\n"])
        self.assertEqual(router.attempt_count(router.parse_log(self.log), "TASK-1"), 4)
        router.prune(self.log, NOW)
        self.assertEqual(router.attempt_count(router.parse_log(self.log), "TASK-1"), 1)

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
                     ["--plan", "a", "b"], ["-h"]):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(router.main(argv, gql=None), 2, argv)
            self.assertIn("usage:", err.getvalue())


class Claim(Base):
    def test_priority_then_age(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=0, created="2026-01-01T00:00:00Z"),
                           issue("TASK-2", "Todo", "researcher", priority=3, created="2026-02-01T00:00:00Z"),
                           issue("TASK-3", "Todo", "researcher", priority=3, created="2026-01-15T00:00:00Z")])
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-3 https://linear.app/x/TASK-3 Deep Research")
        self.assertEqual((fake.issues["TASK-3"]["state"], fake.issues["TASK-3"]["assignee"]), ("In Progress", who("researcher")))

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

    def test_pick_project_is_a_usage_error(self):
        self.assertEqual(self.run_main(FakeLinear([]), "--pick", "--project", "p-dr")[0], 2)

    def test_pick_unknown_role_exits(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main(FakeLinear([]), "--pick", "--role", "ghost")
        self.assertEqual(cm.exception.code, "no role 'ghost' in roles/")

    def test_board_needs_a_role(self):
        fake = FakeLinear([])
        with self.assertRaises(SystemExit):
            router.Board(fake, [], self.tdir, NOW, True, pipeline.load_config(self.config), only=[])
        self.assertEqual(fake.queries, [])

    def test_role_account_missing_in_linear_exits(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "pm")])
        fake.missing = {ROLE["pm"].lower()}   # the users lookup returns no node for these
        with self.assertRaises(SystemExit) as cm:
            self.run_main(fake, "--claim")
        self.assertIn("roles/pm.toml: account", str(cm.exception.code))
        self.assertEqual(fake.mutations, [])

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

    def test_queries_by_id(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=5)), issue("TASK-2", "Todo", "researcher"))
        self.run_main(fake, "--claim")
        flts = [v["f"] for q, v in fake.queries if "issues(filter" in q]
        self.assertTrue(flts)
        self.assertTrue(all(set(f["state"]) == {"id"} and f["state"]["id"]["eq"] in STATES.values() for f in flts))
        text = " ".join(q for q, _ in fake.queries)
        self.assertNotIn("name: { eq", text)
        self.assertNotIn("workflowStates", text)
        self.assertNotIn("state { name }", text)
        self.assertEqual(fake.issues["TASK-2"]["state"], "In Progress")

    def test_state_outside_team_stops_before_changes(self):
        fake = self.fake(issue("TASK-1", "Todo", "researcher"))
        fake.state_ids = [i for i in IDS_BY_KEY.values() if i != IDS_BY_KEY["in_review"]]
        with self.assertRaises(SystemExit) as cm:
            self.run_main(fake, "--claim")
        self.assertIn("[states] not workflow states of team", str(cm.exception.code))
        self.assertEqual(fake.mutations, [])

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

    def test_empty_queue(self):
        self.assertEqual(self.run_main(FakeLinear([]), "--claim"), (0, ""))

    def test_dry_run(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.run_main(fake, "--claim", "--dry-run"), (0, ""))
        self.assertEqual(fake.mutations, [])

    def test_no_mode_recovers_then_claims(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))])
        self.assertEqual(self.run_main(fake, "--pick")[1], "TASK-1 https://linear.app/x/TASK-1")
        self.assertEqual(fake.issues["TASK-1"]["comments"][0], router.INTERRUPTED)


class MultiRole(Base):
    def test_claims_in_sequence(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=2, created="2026-09-01T00:00:00Z"),
                           issue("TASK-2", "Todo", "pm", priority=2, created="2026-09-02T00:00:00Z", project=PD),
                           issue("TASK-3", "Todo", "researcher", priority=1, created="2026-09-03T00:00:00Z")])
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-3 https://linear.app/x/TASK-3 Deep Research")
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-2 https://linear.app/x/TASK-2 Product Design")
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-1 https://linear.app/x/TASK-1 Deep Research")

    def test_claim_priority_then_later_role_then_age(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=2, created="2026-09-01T00:00:00Z"),
                           issue("TASK-2", "Todo", "engineer", priority=2, created="2026-09-03T00:00:00Z"),
                           issue("TASK-3", "Todo", "pm", priority=2, created="2026-09-02T00:00:00Z"),
                           issue("TASK-4", "Todo", "engineer", priority=2, created="2026-09-02T00:00:00Z")])
        self.assertEqual(self.run_main(fake, "--claim")[1].split()[0], "TASK-4")

    def test_resume_prefers_later_role_at_equal_priority(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", priority=2),
                           issue("TASK-2", "In Progress", "pm", priority=2, project=PD)], self.hist)
        self.resumable("TASK-1", "a", 120)
        self.resumable("TASK-2", "b", 60)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-2 b 1 https://linear.app/x/TASK-2 Product Design")


class Blockers(Base):
    def test_blocked_skipped_for_next_ready(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", priority=1, inverse=[
                               blocker("TASK-7"), blocker("TASK-8", "completed"), blocker("TASK-9", "unstarted")]),
                           issue("TASK-2", "Todo", "researcher", priority=2), issue("TASK-3", "Todo", "researcher", priority=3)])
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-7, TASK-9", "pick: TASK-2 (2 in queue)"])
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
        self.assertEqual(self.said(), ["blocked: TASK-2 by TASK-1", "pick: TASK-1 (1 in queue)"])
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
        self.assertEqual(said[1:], ["blocked: TASK-1 by (unreadable)", "blocked: TASK-2 by TASK-7", "pick: TASK-3 (1 in queue)"])
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

    def test_todo_memoized(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")]), issue("TASK-2", "Todo", "researcher")])
        board = router.Board(fake, [], self.tdir, NOW, True, pipeline.load_config(self.config))
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertTrue(board.is_blocked("TASK-1"))
            self.assertEqual([i["identifier"] for i in board.todo()], ["TASK-2"])
            self.assertFalse(board.is_blocked("TASK-2"))
        self.assertEqual(err.getvalue().count("blocked: TASK-1 by TASK-7"), 1)
        self.assertEqual(sum("inverseRelations" in q for q, _ in fake.queries), 1)

    def test_dry_run_logs_blocked(self):
        for argv, out, last in ((("--claim", "--dry-run"), "", "pick: TASK-2 (1 in queue)"),
                                (("--plan", "--dry-run"), "new", "plan: new (1 in queue)")):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7"), blocker("TASK-8", "backlog")]),
                               issue("TASK-2", "Todo", "pm", project=PD)])
            self.assertEqual(self.run_main(fake, *argv), (0, out), argv)
            self.assertEqual(self.said(), ["blocked: TASK-1 by TASK-7, TASK-8", last], argv)
            self.assertEqual(fake.mutations, [], argv)


class FakeShell:
    """active: the busy roles; has-session finds only the exact target =agent-pm-<role> of one."""
    def __init__(self, active=(), probe_five=0.2):
        self.calls, self.five = [], probe_five
        self.busy = {("-t", f"=agent-pm-{r}") for r in active}

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[:2] == ["tmux", "has-session"]:
            return subprocess.CompletedProcess(cmd, 0 if tuple(cmd[2:]) in self.busy else 1)
        if cmd[0] == "claude":
            ev = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": self.five}, "seven_day": {"utilization": 0.1}}}}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(ev) + "\n")
        return subprocess.CompletedProcess(cmd, 0)

    def launches(self):
        return [c for c in self.calls if len(c) > 1 and c[1].endswith("launch.py")]


class Tick(Base):
    def tick(self, fake, *argv, hour=2, shell=None):
        self.sh = shell or FakeShell()
        with open(self.log, "w") as f:
            f.write("\n".join(self.lines) + ("\n" if self.lines else ""))
        err = io.StringIO()
        with redirect_stderr(err), mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}):
            rc = router.main(list(argv), gql=fake, now=NOW, tdir=self.tdir, config=self.config, runs=self.log,
                             sh=self.sh, hour=hour)
            self.path = os.environ["PATH"]
        self.err = self.unmap(err.getvalue())
        with open(self.log) as f:
            self.state = self.unmap(f.read())
        return rc

    def launched(self):
        (launch,) = self.sh.launches()
        return launch[launch.index("--issue") + 1]

    def test_outside_hours(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.tick(fake, hour=12), 0)
        self.assertIn("skip: outside hours", self.err)
        self.assertEqual(self.sh.calls, [])

    def test_hours_boundaries(self):
        for hour, runs in ((0, False), (1, True), (6, True), (7, False), (23, False)):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
            self.tick(fake, hour=hour)
            self.assertEqual(len(self.sh.launches()), int(runs), hour)
            self.assertEqual("skip: outside hours" in self.err, not runs, hour)

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
                                      "--assignee", ROLE["researcher"], "--sid", sid, "--mode", "new"])
        self.assertRegex(self.state, rf"start TASK-1 session={sid} transcript={re.escape(pipeline.transcript('TASK-1', sid, self.tdir))}\n$")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_lock_per_role_exact_match(self):
        sh = FakeShell({"researcher", "engineer"})
        self.assertEqual(router.busy_roles(["researcher", "pm", "engineer"], sh), ["engineer", "researcher"])
        self.assertEqual(sh.calls, [["tmux", "has-session", "-t", f"=agent-pm-{r}"] for r in ("researcher", "pm", "engineer")])
        self.assertEqual(sh(["tmux", "has-session", "-t", "agent-pm-researcher"]).returncode, 1)
        self.tick(FakeLinear([]))
        self.assertCountEqual([c for c in self.sh.calls if c[0] == "tmux"],
                              [["tmux", "has-session", "-t", f"=agent-pm-{r}"] for r in ROLE])

    def test_all_busy_skips_before_linear(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        for argv in ((), ("--dry-run",), ("--now", "--issue", "TASK-1")):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
            self.assertEqual(self.tick(fake, *argv, shell=FakeShell(ROLE)), 0)
            self.assertEqual(self.said(), ["skip: all roles busy (engineer, pm, researcher)"], argv)
            self.assertEqual(fake.queries, [], argv)
            self.assertEqual([c[0] for c in self.sh.calls], ["tmux"] * 3, argv)
            self.assertIn("TASK-8", self.state, argv)

    def test_busy_role_in_progress_left_alone(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "engineer", priority=1),
                           issue("TASK-2", "In Progress", "engineer", updated=ago(hours=3)),
                           issue("TASK-3", "In Progress", "engineer"),
                           issue("TASK-4", "In Progress", "researcher", updated=ago(hours=3))], self.hist)
        self.resumable("TASK-1", "a", 60)
        for m in (390, 380, 370, 360):
            self.add("start", "TASK-3", f"c{m}", m)
        self.tick(fake, shell=FakeShell({"engineer"}))
        self.assertEqual([fake.issues[i]["state"] for i in ("TASK-1", "TASK-2", "TASK-3")], ["In Progress"] * 3)
        self.assertEqual([i for i in ("TASK-1", "TASK-2", "TASK-3") if "comments" in fake.issues[i]], [])
        self.assertEqual(fake.issues["TASK-4"]["comments"], [router.INTERRUPTED])
        self.assertEqual(self.launched(), "TASK-4")
        flts = [v["f"] for q, v in fake.queries if "issues(filter" in q]
        self.assertEqual({tuple(sorted(f["assignee"]["id"]["in"])) for f in flts}, {("u-pm", "u-researcher")})

    def test_busy_role_todo_left_alone(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer", priority=1), issue("TASK-2", "Todo", "engineer", priority=1),
                           issue("TASK-3", "Todo", "pm", priority=2, project=PD)])
        for m in (390, 380, 370, 360):
            self.add("start", "TASK-1", f"a{m}", m)
        self.tick(fake, shell=FakeShell({"engineer"}))
        self.assertEqual([fake.issues[i]["state"] for i in ("TASK-1", "TASK-2")], ["Todo", "Todo"])
        self.assertNotIn("comments", fake.issues["TASK-1"])
        self.assertEqual(self.launched(), "TASK-3")

    def test_idle_role_resumes_before_claim(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "pm", priority=1, project=PD),
                           issue("TASK-2", "In Progress", "researcher", priority=3), issue("TASK-3", "Todo", "engineer", priority=1)], self.hist)
        self.resumable("TASK-1", "a", 120)
        self.resumable("TASK-2", "b", 60)
        self.tick(fake, shell=FakeShell({"pm"}))
        self.assertEqual(self.launched(), "TASK-2")
        self.assertEqual(self.sh.launches()[0][-4:], ["--mode", "resume", "--k", "1"])
        self.assertEqual(fake.issues["TASK-3"]["state"], "Todo")

    def test_idle_role_claim_order(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "pm", priority=1, project=PD),
                           issue("TASK-2", "Todo", "researcher", priority=2, created="2026-09-01T00:00:00Z"),
                           issue("TASK-3", "Todo", "engineer", priority=2, created="2026-09-03T00:00:00Z"),
                           issue("TASK-4", "Todo", "engineer", priority=2, created="2026-09-02T00:00:00Z"),
                           issue("TASK-5", "Todo", "researcher", priority=3, created="2026-08-01T00:00:00Z")])
        self.tick(fake, shell=FakeShell({"pm"}))
        self.assertEqual(self.launched(), "TASK-4")
        self.assertEqual([i for i, x in fake.issues.items() if x["state"] != "Todo"], ["TASK-4"])

    def test_nothing_to_do_with_busy_role(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "In Progress", "engineer", updated=ago(hours=3))])
        self.tick(fake, shell=FakeShell({"engineer"}))
        self.assertEqual(self.said(), ["busy: engineer", "plan: nothing to do", "skip: nothing to do"])
        self.assertEqual([c[0] for c in self.sh.calls], ["tmux"] * 3)
        self.assertEqual([v["e"] for q, v in fake.queries if "users(filter" in q], [ROLE["pm"], ROLE["researcher"]])
        self.assertEqual(fake.mutations, [])

    def test_issue_flag_of_busy_role_skips(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        for argv in (("--now", "--issue", "TASK-1"), ("--now", "--issue", "TASK-1", "--dry-run")):
            fake = FakeLinear([issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "Todo", "researcher")])
            self.assertEqual(self.tick(fake, *argv, shell=FakeShell({"engineer"})), 0)
            self.assertEqual(self.said(), ["busy: engineer", "skip: TASK-1 belongs to busy role engineer"], argv)
            self.assertEqual([v for _, v in fake.queries], [{"f": {"team": {"id": {"eq": TEAM}}, "id": {"eq": "TASK-1"}}}], argv)
            self.assertEqual(fake.mutations, [], argv)
            self.assertEqual([c[0] for c in self.sh.calls], ["tmux"] * 3, argv)
            self.assertIn("TASK-8", self.state, argv)

    def test_issue_flag_of_idle_role_while_another_busy(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer", priority=1), issue("TASK-2", "Todo", "researcher", priority=4)])
        self.tick(fake, "--now", "--issue", "TASK-2", shell=FakeShell({"engineer"}))
        self.assertEqual(self.launched(), "TASK-2")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-2"]["state"]), ("Todo", "In Progress"))

    def test_issue_flag_not_a_role_issue_while_busy(self):
        for ident in ("TASK-9", "TASK-2", "TASK-3"):
            fake = FakeLinear([issue("TASK-1", "Todo", "researcher"), issue("TASK-2", "Todo", "someone"), issue("TASK-3", "Todo")])
            self.tick(fake, "--now", "--issue", ident, shell=FakeShell({"engineer"}))
            self.assertIn(f"pick: {ident} is not a Todo issue assigned to a role account", self.err)
            self.assertEqual((self.sh.launches(), fake.mutations), ([], []))

    def test_dry_run_with_busy_roles(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "Todo", "researcher")])
        self.tick(fake, "--dry-run", shell=FakeShell({"pm", "engineer"}))
        said = self.said()
        self.assertEqual(said[:3], ["busy: engineer, pm", "plan: new (1 in queue)", "plan: new"])
        self.assertRegex(said[3], r"^usage: status=allowed .*\(new allowed\)$")
        self.assertEqual(len(said), 4)
        self.assertEqual([c[0] for c in self.sh.calls].count("claude"), 1)
        self.assertEqual((self.sh.launches(), fake.mutations), ([], []))

    def test_nothing_to_do_skips_probe(self):
        self.tick(FakeLinear([]))
        self.assertIn("skip: nothing to do", self.err)
        self.assertFalse(any(c[0] == "claude" for c in self.sh.calls))

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
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, shell=FakeShell(probe_five=0.95))
        self.assertIn("skip: new blocked by usage", self.err)
        self.assertEqual(self.sh.launches(), [])
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_resume(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.tick(fake)
        (launch,) = self.sh.launches()
        self.assertEqual(launch[2:10], ["--issue", "TASK-1", "--url", "https://linear.app/x/TASK-1", "--project", IDS[DR],
                                        "--assignee", ROLE["researcher"]])
        self.assertEqual(launch[-4:], ["--mode", "resume", "--k", "1"])
        self.assertIn("--sid", launch)
        self.assertTrue(self.state.endswith("resume TASK-1 session=a n=1\n"))

    def test_issue_flag_wins_over_resume(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher"), issue("TASK-2", "Todo", "researcher")], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.tick(fake, "--now", "--issue", "TASK-2")
        (launch,) = self.sh.launches()
        self.assertEqual(launch[launch.index("--issue") + 1], "TASK-2")

    def test_issue_flag_still_recovers(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)), issue("TASK-2", "Todo", "researcher")])
        self.tick(fake, "--now", "--issue", "TASK-1")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.INTERRUPTED])
        (launch,) = self.sh.launches()
        self.assertEqual(launch[launch.index("--issue") + 1], "TASK-1")

    def test_issue_flag_not_in_todo(self):
        self.tick(FakeLinear([issue("TASK-1", "Todo", "researcher")]), "--now", "--issue", "TASK-9")
        self.assertIn("pick: TASK-9 is not a Todo issue assigned to a role account", self.err)
        self.assertEqual(self.sh.launches(), [])

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

    def test_dry_run(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, "--dry-run", hour=12)
        self.assertIn("plan: new", self.err)
        self.assertIn("usage: status=allowed", self.err)
        self.assertEqual((self.sh.launches(), fake.mutations), ([], []))
        self.assertIn("TASK-8", self.state)

    def test_state_log_has_only_state_lines(self):
        self.tick(FakeLinear([issue("TASK-1", "Todo", "researcher")]))
        self.assertEqual([line.split()[2] for line in self.state.splitlines()], ["start"])

    def test_bad_usage(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(router.main(["--issue", "TASK-1"], gql=None), 2)


if __name__ == "__main__":
    unittest.main()
