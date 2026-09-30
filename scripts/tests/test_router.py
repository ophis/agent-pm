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
from board_ids import HEADER, STATES as IDS_BY_KEY, team_node  # noqa: E402
import pipeline  # noqa: E402
import router  # noqa: E402

NOW = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
ME, USER = "agent", "user"
STATES = {"Todo": IDS_BY_KEY["todo"], "In Progress": IDS_BY_KEY["in_progress"], "In Review": IDS_BY_KEY["in_review"]}
NAMES = {i: n for n, i in STATES.items()}
DR, PD = "Deep Research", "Product Design"
PROJECTS = (DR, PD, "Engineering")
IDS = {DR: "p-dr", PD: "p-pd", "Engineering": "p-eng"}
# Role and task files must exist under the repo root.
CONFIG = HEADER + """[projects.p-dr]
next = "p-pd"
role = "researcher"
task = "deep-research"
[projects.p-pd]
prefix = "PRD"
"""
PD_RUNNABLE = 'role = "pm"\ntask = "product-design"\n'
ROLE_IDS = {r.account: f"role:{n}" for n, r in pipeline.registry()[0].items()}


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat().replace("+00:00", "Z")


class FakeLinear:
    def __init__(self, issues, history=None):
        self.issues = {i["identifier"]: i for i in issues}
        self.history = {} if history is None else history
        self.mutations = []
        self.queries = []
        self.state_ids = None
        self.unknown = set()

    def __call__(self, query, **v):
        self.queries.append((query, v))
        if "viewer" in query:
            return {"viewer": {"id": ME}}
        if query == pipeline.Q_TEAM:
            return {"teams": {"nodes": [team_node(self.state_ids, [(IDS[p], p) for p in PROJECTS])]}}
        if "users(filter" in query:
            return {"users": {"nodes": [{"id": uid}] if (uid := {"me@x.com": USER, **ROLE_IDS}.get(v["e"])) and v["e"] not in self.unknown else []}}
        if "history" in query:
            return {"issue": {"history": {"nodes": self.history.get(v["i"], [])}}}
        if "mutation" in query:
            self.mutations.append((query, v))
            issue = self.issues[v["i"]]
            if "commentCreate" in query:
                issue.setdefault("comments", []).append(v["b"])
            else:
                upd = v.get("u") or {"stateId": v["s"], "assigneeId": v["a"]}
                issue["state"] = NAMES[upd["stateId"]]
                if "assigneeId" in upd:
                    issue["assignee"] = upd["assigneeId"]
            return {}
        if "issues(filter" in query:
            f = v["f"]
            want = f.get("assignee", {}).get("id", {}).get("eq")
            return {"issues": {"nodes": [{k: x for k, x in i.items() if k != "state"} for i in self.issues.values()
                                         if i["state"] == NAMES[f["state"]["id"]["eq"]] and (want is None or i["assignee"] == want)
                                         and i["project"]["id"] in f["project"]["id"]["in"]]}}
        if "state { id }" in query:
            return {"issue": {"state": {"id": STATES[self.issues[v["i"]]["state"]]}}}
        raise AssertionError(query)


def issue(ident, state, assignee=None, updated=None, priority=0, created="2026-09-01T00:00:00Z", project=DR):
    # id == identifier so mutations and history can be keyed by either
    return {"id": ident, "identifier": ident, "url": f"https://linear.app/x/{ident}", "state": state, "project": {"id": IDS[project], "name": project},
            "assignee": assignee, "priority": priority, "createdAt": created, "updatedAt": updated or ago(minutes=5)}


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

    def moved(self, ident, minutes_ago, state=STATES["In Progress"], actor=ME):
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
        fake = self.fake(issue("TASK-1", "In Progress", ME, updated=ago(hours=3)))
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
        fake = FakeLinear([issue("TASK-1", "In Review", ME)])
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))

    def test_new(self):
        fake = FakeLinear([issue("TASK-1", "Todo")])
        self.assertEqual(self.run_main(fake, "--plan")[1], "new")
        self.assertEqual(fake.mutations, [])

    def test_resume_has_no_session_cap(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME, updated=ago(hours=5)), issue("TASK-2", "Todo"))
        self.resumable("TASK-1", "sid1", 300)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 sid1 1 https://linear.app/x/TASK-1 Deep Research")
        self.add("resume", "TASK-1", "sid1", 200)
        self.add("resume", "TASK-1", "sid1", 100)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 sid1 3 https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(fake.mutations, [], "a candidate is never moved, even when stale")

    def test_attempt_cap_beats_resume(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME, updated=ago(hours=5)))
        self.resumable("TASK-1", "sid1", 300)
        for m in (200, 150, 100):
            self.add("resume", "TASK-1", "sid1", m)
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])

    def test_live_session_not_resumed_or_recovered(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME, updated=ago(hours=5)))
        self.add("start", "TASK-1", "sid1", 300)
        self.touch("TASK-1", "sid1", 5, "subagents/workflows/r/journal.jsonl")
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.mutations, [])

    def test_original_bug_live_issue_then_new_issue(self):
        a = issue("TASK-1", "In Progress", ME, updated=ago(hours=3))
        fake = self.fake(a, issue("TASK-2", "Todo"))
        self.resumable("TASK-1", "a", 200)
        self.touch("TASK-1", "a", 5)
        self.assertEqual(self.run_main(fake, "--plan")[1], "new")
        self.assertEqual(fake.mutations, [])
        fake.issues["TASK-2"].update(state="In Progress", assignee=ME)
        self.resumable("TASK-2", "b", 60)
        self.touch("TASK-1", "a", 40)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 a 1 https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(fake.mutations, [])

    def order(self, *specs):
        """specs: (ident, priority, start minutes ago); returns the resumed issue."""
        fake = self.fake(*[issue(i, "In Progress", ME, priority=p) for i, p, _ in specs])
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
        fake = self.fake(issue("TASK-1", "In Progress", ME, priority=1), issue("TASK-2", "In Progress", ME, priority=4))
        self.add("start", "TASK-1", "old", 400)
        self.touch("TASK-1", "old", 300)
        self.moved("TASK-1", 60, actor=USER)
        self.resumable("TASK-2", "b", 100)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-2 b 1 https://linear.app/x/TASK-2 Deep Research")

    def test_sid_without_start_sorts_by_first_line(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME, priority=2), issue("TASK-2", "In Progress", ME, priority=2))
        self.add("resume", "TASK-1", "a", 300)
        self.add("resume", "TASK-1", "a", 50)
        self.touch("TASK-1", "a", 40)
        self.resumable("TASK-2", "b", 200)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 a 3 https://linear.app/x/TASK-1 Deep Research")

    def walk_fixture(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME, priority=2), issue("TASK-4", "In Progress", ME, priority=2),
                         issue("TASK-3", "In Progress", ME, priority=3), issue("TASK-5", "In Progress", ME, priority=4),
                         issue("TASK-6", "In Progress", ME, priority=4), issue("TASK-9", "Todo", priority=1))
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
        fake = self.fake(issue("TASK-1", "In Progress", ME, priority=1), issue("TASK-3", "In Progress", ME, priority=3))
        self.resumable("TASK-1", "a", 300)
        self.touch("TASK-1", "a", 5)
        self.resumable("TASK-3", "c", 100)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-3 c 1 https://linear.app/x/TASK-3 Deep Research")
        self.assertEqual(fake.mutations, [])

    def test_sid_without_transcript(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME, updated=ago(hours=3)))
        self.add("start", "TASK-1", "a", 10)
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.mutations, [])
        self.lines = []
        self.add("start", "TASK-1", "a", 40)
        self.assertEqual(self.run_main(fake, "--plan")[1], "new")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("Todo", None))

    def test_no_sid_keeps_two_hour_rule(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", ME, updated=ago(hours=3)),
                           issue("TASK-2", "In Progress", ME, updated=ago(hours=1))])
        self.run_main(fake, "--plan")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")
        self.assertEqual(fake.issues["TASK-1"]["comments"][0], router.INTERRUPTED)
        self.assertEqual(fake.issues["TASK-2"]["state"], "In Progress")

    def test_stale_sid_goes_to_two_hour_rule(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME, updated=ago(minutes=220)))
        self.add("start", "TASK-1", "old", 400)
        self.touch("TASK-1", "old", 400)
        self.moved("TASK-1", 220, actor=USER)
        self.assertEqual(self.run_main(fake, "--plan")[1], "new")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_tolerance(self):
        for start, want in ((100.05, "resume"), (106, "")):
            self.lines, self.hist = [], {}
            fake = self.fake(issue("TASK-1", "In Progress", ME))
            self.moved("TASK-1", 100)
            self.add("start", "TASK-1", "a", start)
            self.touch("TASK-1", "a", 40)
            self.assertEqual(self.run_main(fake, "--plan")[1].split(" ")[0], want)

    def test_no_move_in_history_is_current(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME))
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.add("start", "TASK-1", "a", 100)
        self.touch("TASK-1", "a", 40)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 a 1 https://linear.app/x/TASK-1 Deep Research")

    def test_no_current_sid_never_capped(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME, updated=ago(hours=1)))
        for m in (600, 550, 500, 450):
            self.add("start", "TASK-1", f"s{m}", m)
        self.moved("TASK-1", 60, actor=USER)
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.mutations, [])

    def test_launch_failures_end_in_review(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME))
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
        self.moved("TASK-1", 700)
        self.add("start", "TASK-1", "old", 699)
        for m in (650, 600, 550):
            self.add("resume", "TASK-1", "old", m)
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.resumable("TASK-1", "new", 100)
        self.add("resume", "TASK-1", "new", 60)

    def test_user_reset_resumes_new_sid(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME))
        self.reset_fixture()
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-1 new 2 https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(fake.mutations, [])

    def test_user_reset_orders_by_new_sid(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME, priority=2), issue("TASK-2", "In Progress", ME, priority=2))
        self.reset_fixture()
        self.resumable("TASK-2", "b", 200)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-2 b 1 https://linear.app/x/TASK-2 Deep Research")

    def test_not_ours_not_candidate(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", USER, updated=ago(hours=5))])
        self.add("start", "TASK-1", "sid1", 300)
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.mutations, [])

    def test_attempt_cap_moves_to_review(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", ME, updated=ago(minutes=40))])
        for i, sid in enumerate(["a", "b", "c"]):
            self.add("start", "TASK-1", sid, 400 - i * 50)
        self.add("resume", "TASK-1", "c", 200)
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])

    def test_attempt_cap_assigns_reviewer(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "In Progress", ME, updated=ago(minutes=40))])
        for i, sid in enumerate(["a", "b", "c", "d"]):
            self.add("start", "TASK-1", sid, 400 - i * 50)
        self.run_main(fake, "--plan")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("In Review", USER))

    def test_interrupted_unassigns_even_with_reviewer(self):
        self.config = self.write_config('human_members = ["me@x.com"]\n' + CONFIG)
        fake = FakeLinear([issue("TASK-1", "In Progress", ME, updated=ago(hours=5))])
        self.run_main(fake, "--plan")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]), ("Todo", None))

    def test_unknown_reviewer_fails_loud(self):
        self.config = self.write_config('human_members = ["nobody@x.com"]\n' + CONFIG)
        with self.assertRaises(SystemExit) as e:
            self.run_main(FakeLinear([]), "--plan")
        self.assertIn("nobody@x.com", str(e.exception))

    def capped(self, hist):
        fake = FakeLinear([issue("TASK-1", "In Progress", ME, updated=ago(hours=3))], {"TASK-1": hist})
        for i, sid in enumerate("abcd"):
            self.add("start", "TASK-1", sid, 400 - i * 50)
        self.touch("TASK-1", "d", 200)
        return fake, self.run_main(fake, "--plan")[1]

    def test_attempt_cap_reset_by_user(self):
        fake, out = self.capped([{"createdAt": ago(minutes=380), "actorId": USER, "toStateId": STATES["Todo"]}])
        self.assertEqual(out, "resume TASK-1 d 1 https://linear.app/x/TASK-1 Deep Research")
        self.assertEqual(fake.mutations, [])

    def test_attempt_cap_not_reset_by_agent_or_other_moves(self):
        fake, out = self.capped([{"createdAt": ago(minutes=380), "actorId": ME, "toStateId": STATES["Todo"]},
                                 {"createdAt": ago(minutes=370), "actorId": USER, "toStateId": STATES["In Progress"]},
                                 {"createdAt": ago(minutes=360), "actorId": None, "toStateId": STATES["Todo"]}])
        self.assertEqual(out, "")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")

    def test_attempt_cap_not_reset_by_role_account(self):
        pm = ROLE_IDS["frank.agent.w+pm@gmail.com"]  # not the runnable project's role
        fake, out = self.capped([{"createdAt": ago(minutes=380), "actorId": pm, "toStateId": STATES["Todo"]}])
        self.assertEqual(out, "")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")

    def test_attempt_cap_reset_by_other_member(self):
        fake, out = self.capped([{"createdAt": ago(minutes=380), "actorId": "member", "toStateId": STATES["Todo"]}])
        self.assertEqual(out, "resume TASK-1 d 1 https://linear.app/x/TASK-1 Deep Research")

    def test_unknown_role_account_fails_loud(self):
        fake = FakeLinear([])
        fake.unknown = {"frank.agent.w+engineer@gmail.com"}
        with self.assertRaises(SystemExit) as e:
            self.run_main(fake, "--plan")
        self.assertIn("role accounts not found in Linear: frank.agent.w+engineer@gmail.com", str(e.exception))

    def test_agents_cover_every_role_under_project_filter(self):
        board = router.Board(FakeLinear([]), [], self.tdir, NOW, True, pipeline.load_config(self.config), only="p-dr")
        self.assertEqual(board.agents, {ME, *ROLE_IDS.values()})

    def test_one_history_fetch_per_issue(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", ME)])
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
        fake = FakeLinear([issue("TASK-1", "Todo", priority=0, created="2026-01-01T00:00:00Z"),
                           issue("TASK-2", "Todo", priority=3, created="2026-02-01T00:00:00Z"),
                           issue("TASK-3", "Todo", priority=3, created="2026-01-15T00:00:00Z")])
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-3 https://linear.app/x/TASK-3 Deep Research")
        self.assertEqual((fake.issues["TASK-3"]["state"], fake.issues["TASK-3"]["assignee"]), ("In Progress", ME))

    def test_capped_todo_goes_to_review(self):
        fake = FakeLinear([issue("TASK-1", "Todo", priority=1), issue("TASK-2", "Todo", priority=2)])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])

    def test_capped_todo_reset_by_user(self):
        hist = {"TASK-1": [{"createdAt": ago(minutes=200), "actorId": USER, "toStateId": STATES["Todo"]}]}
        fake = FakeLinear([issue("TASK-1", "Todo")], hist)
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-1 https://linear.app/x/TASK-1 Deep Research")

    def test_queries_by_id(self):
        fake = self.fake(issue("TASK-1", "In Progress", ME, updated=ago(hours=5)), issue("TASK-2", "Todo"))
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
        fake = self.fake(issue("TASK-1", "Todo"))
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

        fake = Racy([issue("TASK-1", "Todo")])
        self.assertEqual(self.run_main(fake, "--claim"), (0, ""))
        self.assertIn("claim: TASK-1 is no longer Todo; skipping", self.err)
        self.assertEqual(fake.mutations, [])

    def test_empty_queue(self):
        self.assertEqual(self.run_main(FakeLinear([]), "--claim"), (0, ""))

    def test_dry_run(self):
        fake = FakeLinear([issue("TASK-1", "Todo")])
        self.assertEqual(self.run_main(fake, "--claim", "--dry-run"), (0, ""))
        self.assertEqual(fake.mutations, [])

    def test_no_mode_recovers_then_claims(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", ME, updated=ago(hours=3))])
        self.assertEqual(self.run_main(fake, "--pick")[1], "TASK-1 https://linear.app/x/TASK-1")
        self.assertEqual(fake.issues["TASK-1"]["comments"][0], router.INTERRUPTED)


class MultiProject(Base):
    def setUp(self):
        super().setUp()
        self.config = self.write_config(CONFIG + PD_RUNNABLE)

    def test_claim_priority_then_later_stage_then_age(self):
        fake = FakeLinear([issue("TASK-1", "Todo", priority=2, created="2026-09-01T00:00:00Z"),
                           issue("TASK-2", "Todo", priority=2, created="2026-09-02T00:00:00Z", project=PD),
                           issue("TASK-3", "Todo", priority=1, created="2026-09-03T00:00:00Z")])
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-3 https://linear.app/x/TASK-3 Deep Research")
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-2 https://linear.app/x/TASK-2 Product Design")
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-1 https://linear.app/x/TASK-1 Deep Research")

    def test_resume_prefers_later_stage_at_equal_priority(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", ME, priority=2),
                           issue("TASK-2", "In Progress", ME, priority=2, project=PD)], self.hist)
        self.resumable("TASK-1", "a", 120)
        self.resumable("TASK-2", "b", 60)
        self.assertEqual(self.run_main(fake, "--plan")[1], "resume TASK-2 b 1 https://linear.app/x/TASK-2 Product Design")

    def test_non_runnable_project_ignored(self):
        self.config = self.write_config(CONFIG)
        fake = FakeLinear([issue("TASK-1", "Todo", project=PD),
                           issue("TASK-2", "In Progress", ME, updated=ago(hours=3), project=PD)])
        self.assertEqual(self.run_main(fake, "--plan"), (0, ""))
        self.assertEqual(fake.mutations, [])

    def test_renamed_project_still_matches(self):
        fake = FakeLinear([issue("TASK-1", "Todo")])
        fake.issues["TASK-1"]["project"]["name"] = "Research (renamed)"
        self.assertEqual(self.run_main(fake, "--claim")[1], "TASK-1 https://linear.app/x/TASK-1 Research (renamed)")

    def test_pick_project_filter(self):
        fake = FakeLinear([issue("TASK-1", "Todo", priority=1, project=PD), issue("TASK-2", "Todo", priority=3)])
        self.assertEqual(self.run_main(fake, "--pick", "--project", IDS[DR])[1], "TASK-2 https://linear.app/x/TASK-2")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_runnable_project_missing_in_linear_exits(self):
        self.config = self.write_config(CONFIG + "[projects.p-ghost]\n" + PD_RUNNABLE)
        with self.assertRaises(SystemExit):
            self.run_main(FakeLinear([]), "--plan")

class FakeShell:
    def __init__(self, active=False, probe_five=0.2, prune_fails=False):
        self.calls, self.active, self.five = [], active, probe_five

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[:2] == ["tmux", "has-session"]:
            return subprocess.CompletedProcess(cmd, 0 if self.active else 1)
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

    def test_outside_hours(self):
        fake = FakeLinear([issue("TASK-1", "Todo")])
        self.assertEqual(self.tick(fake, hour=12), 0)
        self.assertIn("skip: outside hours", self.err)
        self.assertEqual(self.sh.calls, [])

    def test_hours_boundaries(self):
        for hour, runs in ((0, False), (1, True), (6, True), (7, False), (23, False)):
            fake = FakeLinear([issue("TASK-1", "Todo")])
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
        fake = FakeLinear([issue("TASK-1", "Todo")])
        self.tick(fake, "--now", hour=12)
        (launch,) = self.sh.launches()
        sid = launch[launch.index("--sid") + 1]
        self.assertEqual(launch[0], sys.executable)
        self.assertEqual(launch[2:], ["--issue", "TASK-1", "--url", "https://linear.app/x/TASK-1", "--project", IDS[DR],
                                      "--sid", sid, "--mode", "new"])
        self.assertRegex(self.state, rf"start TASK-1 session={sid} transcript={re.escape(pipeline.transcript('TASK-1', sid, self.tdir))}\n$")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_lock_skips(self):
        fake = FakeLinear([issue("TASK-1", "Todo")])
        self.tick(fake, shell=FakeShell(active=True))
        self.assertIn("skip: previous run still active", self.err)
        self.assertEqual(self.sh.launches(), [])

    def test_nothing_to_do_skips_probe(self):
        self.tick(FakeLinear([]))
        self.assertIn("skip: nothing to do", self.err)
        self.assertFalse(any(c[0] == "claude" for c in self.sh.calls))

    def test_usage_blocked(self):
        fake = FakeLinear([issue("TASK-1", "Todo")])
        self.tick(fake, shell=FakeShell(probe_five=0.95))
        self.assertIn("skip: new blocked by usage", self.err)
        self.assertEqual(self.sh.launches(), [])
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_resume(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", ME)], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.tick(fake)
        (launch,) = self.sh.launches()
        self.assertEqual(launch[-4:], ["--mode", "resume", "--k", "1"])
        self.assertIn("--sid", launch)
        self.assertTrue(self.state.endswith("resume TASK-1 session=a n=1\n"))

    def test_issue_flag_wins_over_resume(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", ME), issue("TASK-2", "Todo")], self.hist)
        self.resumable("TASK-1", "a", 60)
        self.tick(fake, "--now", "--issue", "TASK-2")
        (launch,) = self.sh.launches()
        self.assertEqual(launch[launch.index("--issue") + 1], "TASK-2")

    def test_issue_flag_still_recovers(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", ME, updated=ago(hours=3)), issue("TASK-2", "Todo")])
        self.tick(fake, "--now", "--issue", "TASK-1")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.INTERRUPTED])
        (launch,) = self.sh.launches()
        self.assertEqual(launch[launch.index("--issue") + 1], "TASK-1")

    def test_issue_flag_not_in_todo(self):
        self.tick(FakeLinear([issue("TASK-1", "Todo")]), "--now", "--issue", "TASK-9")
        self.assertIn("pick: TASK-9 is not a Todo issue in a runnable project", self.err)
        self.assertEqual(self.sh.launches(), [])

    def test_issue_flag_claims_that_issue(self):
        fake = FakeLinear([issue("TASK-1", "Todo", priority=1), issue("TASK-2", "Todo", priority=4)])
        self.tick(fake, "--now", "--issue", "TASK-2")
        (launch,) = self.sh.launches()
        self.assertEqual(launch[launch.index("--issue") + 1], "TASK-2")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_prune_runs_before_plan(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        self.tick(FakeLinear([]))
        self.assertNotIn("TASK-8", self.state)

    def test_prune_failure_continues(self):
        fake = FakeLinear([issue("TASK-1", "Todo")])
        with mock.patch.object(router, "prune", side_effect=OSError("disk")):
            self.tick(fake)
        self.assertIn("skip: prune failed", self.err)
        self.assertEqual(len(self.sh.launches()), 1)

    def test_dry_run(self):
        self.add("start", "TASK-8", "old", 60 * 24 * 8)
        fake = FakeLinear([issue("TASK-1", "Todo")])
        self.tick(fake, "--dry-run", hour=12)
        self.assertIn("plan: new", self.err)
        self.assertIn("usage: status=allowed", self.err)
        self.assertEqual((self.sh.launches(), fake.mutations), ([], []))
        self.assertIn("TASK-8", self.state)

    def test_state_log_has_only_state_lines(self):
        self.tick(FakeLinear([issue("TASK-1", "Todo")]))
        self.assertEqual([line.split()[2] for line in self.state.splitlines()], ["start"])

    def test_bad_usage(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(router.main(["--issue", "TASK-1"], gql=None), 2)


if __name__ == "__main__":
    unittest.main()
