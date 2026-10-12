"""orchestrator router.py's decisions, ported from the old router_test (Plan, Claim, Blockers, board-level TaskLabels,
Gate over Usage) onto config.load() and a fake Driver api, plus FR-46 to FR-48, FR-56 and FR-60."""

from new.tests.unit.orchestrator import run_fixtures
from new.tests.unit.orchestrator.board_ids import ACCOUNTS, HEADER, TASK_GROUP, TEAM, team_node
from new.tests.unit.orchestrator.board_ids import STATES as IDS_BY_KEY
from new.tests.unit.orchestrator.board_ids import role as role_table
from new.tests.unit.orchestrator.fake_api import FakeApi
from new.tests.unit.orchestrator.run_fixtures import HARNESS, logged, show

import config

import argparse
import functools
import io
import json
import os
import re
import tempfile
import uuid
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta, timezone
from unittest import mock

import linear
import router
import runs
from driver import api

NOW = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
EAST, WEST = timezone(timedelta(hours=8)), timezone(timedelta(hours=-4))
AGENT, USER = "agent", "user"
STATES = {
    "Todo": IDS_BY_KEY["todo"],
    "In Progress": IDS_BY_KEY["in_progress"],
    "In Review": IDS_BY_KEY["in_review"],
}
NAMES = {i: n for n, i in STATES.items()}
DR, PD = "Deep Research", "Product Design"
IDS = {DR: "p-dr", PD: "p-pd"}
ROLE = ACCOUNTS
RUNTIMES = {"researcher": "a", "pm": "b", "engineer": "a"}
OVERLAY = '[core]\nruntime = "a"\n\n[core.roles.pm]\nruntime = "b"\n'
CONFIG = (
    HEADER
    + role_table("researcher", 'next = "pm"')
    + role_table("pm", 'next = "engineer"')
    + role_table("engineer")
    + OVERLAY
)
HUMANS = 'human_members = ["me@x.com"]\n'
LIGHT, DEEP, STRAY, ORPHAN_LABEL = (
    f"00000000-0000-4000-8000-0000000000{n}" for n in (21, 22, 23, 24)
)
TASK_LABELS = f'[task_labels]\nlight-research = "{LIGHT}"\ndeep-research = "{DEEP}"\nproduct-design = "{ORPHAN_LABEL}"\n'
RECHECK = (
    "query($i: String!) { issue(id: $i) { state { id } assignee { id email } project { id name } "
    "labels { nodes { id name parent { id } } } } }"
)
NO = api.SessionStatus("no", None, "")


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def who(role):
    return None if role is None else {"id": f"u-{role}", "email": ROLE.get(role, f"{role}@x.com")}


def reading(five=10.0, weekly=10.0):
    return api.Usage(
        None if five is None else api.Window(five, None),
        None if weekly is None else api.Window(weekly, None),
    )


class FakeLinear:
    def __init__(self, issues, history=None):
        self.issues = {i["identifier"]: i for i in issues}
        self.history = {} if history is None else history
        self.mutations, self.queries, self.services = [], [], []
        self.todo_error = False
        self.unreadable = set()
        self.refuse = set()
        self.group = {
            "isGroup": True,
            "children": {"nodes": [{"id": i} for i in (LIGHT, DEEP, ORPHAN_LABEL)]},
        }

    def __call__(self, query, *, service, **v):
        self.queries.append((query, v))
        self.services.append(service)
        if query == linear.Q_TEAM:
            return {"teams": {"nodes": [team_node()]}}
        if query == linear.Q_TASK_GROUP:
            return {"issueLabel": self.group}
        if "users(filter" in query:
            users = {
                "me@x.com": USER,
                "b@x.com": "user-b",
                **{a.lower(): f"u-{r}" for r, a in ROLE.items()},
            }
            return {
                "users": {"nodes": [{"id": u} for e, u in users.items() if v["e"].lower() == e]}
            }
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
            drop = (
                {"state", "labels"}
                if "inverseRelations" in query
                else {"state", "labels", "inverseRelations"}
            )
            return {
                "issues": {
                    "nodes": [
                        {k: x for k, x in i.items() if k not in drop}
                        for i in self.issues.values()
                        if f["team"]["id"]["eq"] == TEAM
                        and f["project"] == {"null": False}
                        and i["project"]
                        and i["assignee"]
                        and i["assignee"]["id"] in f["assignee"]["id"]["in"]
                        and i["state"] == NAMES[f["state"]["id"]["eq"]]
                    ]
                }
            }
        if "issue(id:" in query and "inverseRelations" in query:
            if v["i"] in self.unreadable:
                raise SystemExit("linear api error: [{'message': 'Entity not found'}]")
            return {"issue": {"inverseRelations": self.issues[v["i"]]["inverseRelations"]}}
        if query == RECHECK:
            i = self.issues[v["i"]]
            return {
                "issue": {
                    "state": {"id": STATES[i["state"]]},
                    "assignee": i["assignee"],
                    "project": i["project"],
                    "labels": {"nodes": i["labels"]},
                }
            }
        raise AssertionError(query)

    def reads(self, state="Todo"):
        return [
            q
            for q, v in self.queries
            if "issues(filter" in q and v["f"].get("state") == {"id": {"eq": STATES[state]}}
        ]


def op(query):
    return re.search(r"\{ (\w+)\(", query).group(1)


def writes(fake):
    return [
        ("read" if q == linear.Q_ISSUE_STATE else op(q), v["i"])
        for q, v in fake.queries
        if q == linear.Q_ISSUE_STATE or "mutation" in q
    ]


def issue(
    ident,
    state,
    role=None,
    updated=None,
    priority=0,
    created="2026-09-01T00:00:00Z",
    project=DR,
    inverse=(),
    labels=(),
):
    # id == identifier so mutations and history can be keyed by either
    return {
        "id": ident,
        "identifier": ident,
        "url": f"https://linear.app/x/{ident}",
        "state": state,
        "project": project and {"id": IDS[project], "name": project},
        "assignee": who(role),
        "priority": priority,
        "createdAt": created,
        "updatedAt": updated or ago(minutes=5),
        "inverseRelations": {"nodes": list(inverse)},
        "labels": list(labels),
    }


def blocker(ident, state="started", kind="blocks"):
    return {"type": kind, "issue": ident and {"identifier": ident, "state": {"type": state}}}


def label(name, id, group=TASK_GROUP):
    return {"id": id, "name": name, "parent": group and {"id": group}}


def two():
    return FakeLinear(
        [
            issue("TASK-1", "Todo", "researcher", priority=1),
            issue("TASK-2", "Todo", "pm", priority=2, project=PD),
        ]
    )


class Moved(FakeLinear):
    def __init__(self, issues, to):
        super().__init__(issues)
        self.to = to

    def __call__(self, query, **v):
        out = super().__call__(query, **v)
        if "issues(filter" in query:
            for i in self.issues.values():
                i["state"] = self.to
        return out


class Changed(FakeLinear):
    """Each list read is followed by `after`'s edits ({ident: fields}): a change before the claim's recheck."""

    def __init__(self, issues, after):
        super().__init__(issues)
        self.after = after

    def __call__(self, query, **v):
        out = super().__call__(query, **v)
        if "issues(filter" in query:
            for ident, fields in self.after.items():
                self.issues[ident].update(fields)
        return out


class Base(run_fixtures.Base):
    def setUp(self):
        super().setUp()
        self.write(self.local, f'work_dir = "{self.work}"\n')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.config_path = os.path.join(self.tmp, "config.toml")
        self.write_config(CONFIG)
        self.lines, self.hist, self.names = [], {}, {}
        self.logs, self.mark, self.raw, self.taken = None, 0, "", None
        self.api = FakeApi()
        patch = self.api.patch()
        patch.start()
        self.addCleanup(patch.stop)

    def write_config(self, text):
        self.write(self.config_path, text)

    def sid(self, name):
        s = str(uuid.uuid5(uuid.NAMESPACE_OID, name))
        self.names[s] = name
        return s

    def unmap(self, text):
        for s, name in self.names.items():
            text = text.replace(s, name)
        return text

    def fake(self, *issues):
        return FakeLinear(issues, self.hist)

    def moved(self, ident, minutes_ago, state=STATES["In Progress"], actor=AGENT):
        self.hist.setdefault(ident, []).insert(
            0, {"createdAt": ago(minutes=minutes_ago), "actorId": actor, "toStateId": state}
        )

    def resumable(self, ident, sid, minutes_ago, role="researcher"):
        self.moved(ident, minutes_ago + 1)
        self.add("start", ident, sid, minutes_ago, role)
        self.touch(sid, 40)

    def row(self, kind, ident, sid, role="researcher", runtime=None, **ago):
        return json.dumps(
            {
                "ts": (NOW - timedelta(**ago)).astimezone(WEST).isoformat(timespec="seconds"),
                "kind": kind,
                "issue": ident,
                "sid": self.sid(sid),
                "role": role,
                "runtime": runtime or RUNTIMES.get(role, "a"),
            }
        )

    def add(self, kind, ident, sid, minutes_ago, role="researcher", runtime=None):
        self.lines.append(self.row(kind, ident, sid, role, runtime, minutes=minutes_ago))

    def limit(self, runtime, resets_at, ident="TASK-9"):
        self.lines.append(
            json.dumps(
                {
                    "ts": NOW.astimezone(WEST).isoformat(timespec="seconds"),
                    "kind": "limit",
                    "issue": ident,
                    "sid": self.sid("limited"),
                    "runtime": runtime,
                    "resets_at": resets_at,
                }
            )
        )

    def touch(self, sid, minutes_ago):
        # the session's transcript, last written minutes_ago
        at = (NOW - timedelta(minutes=minutes_ago)).timestamp()
        self.api.statuses[self.sid(sid)] = api.SessionStatus("yes", at, f"resume {sid}")

    def write_runs(self):
        self.write(self.runs, "".join(f"{line}\n" for line in self.lines))
        for e in runs.parse_log(self.runs):
            self.api.statuses.setdefault(e.sid, NO)

    def log_dir(self, again):
        if not again or self.logs is None:
            self.logs = tempfile.mkdtemp(dir=self.tmp)
        self.mark = len(logged(self.logs))
        return mock.patch.object(linear, "LOGS_DIR", self.logs)

    def board(self, fake, use, dry=False, gate=False, again=False):
        self.write_runs()
        cfg = config.load(path=self.config_path)
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(again):
            out = use(
                router.Board(
                    functools.partial(fake, service=HARNESS),
                    runs.parse_log(self.runs),
                    NOW,
                    dry,
                    cfg,
                    router.Gate(self.runs, NOW, dry) if gate else None,
                )
            )
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        return out

    def plan(self, fake, dry=False, again=False):
        def use(b):
            if cands := b.recover():
                return ("resume", *cands[0])
            return ("new",) if b.queue() else None

        run = self.board(fake, use, dry, again=again)
        if run and run[0] == "resume":
            _, issue, sid = run
            return self.unmap(
                f"resume {issue['identifier']} {sid} {issue['url']} {issue['project']['name']}"
            )
        return "new" if run else ""

    def claim(self, fake, dry=False, full=(), recover=False, gate=False, again=False):
        def use(b):
            recover and b.recover()
            return b.take(full=full)

        self.taken = taken = self.board(fake, use, dry, gate, again)
        return (
            taken
            and f"{taken[0]['identifier']} {taken[0]['url']} {taken[0]['project']['name']}"
            or ""
        )

    def said(self):
        events = logged(self.logs)[self.mark :] + [
            json.loads(x) for x in self.raw.splitlines() if x.startswith("{")
        ]
        self.assertEqual({e["src"] for e in events} - {"router"}, set())
        return [self.unmap(show(e)) for e in events]


class Plan(Base):
    def test_new_or_nothing_without_a_change(self):
        rows = (
            ("nothing", [issue("TASK-1", "In Review", "researcher")], ""),
            ("new", [issue("TASK-1", "Todo", "researcher")], "new"),
            (
                "other assignees not recovered",
                [
                    issue("TASK-1", "In Progress", "someone", updated=ago(hours=3)),
                    issue("TASK-2", "In Progress", None, updated=ago(hours=3)),
                ],
                "",
            ),
        )
        for name, issues_, out in rows:
            with self.subTest(name):
                fake = FakeLinear(issues_)
                self.assertEqual(self.plan(fake), out)
                self.assertEqual(fake.mutations, [])
                self.assertEqual(set(fake.services), {HARNESS})

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
        for others, out in (
            ((), ""),
            ((issue("TASK-2", "Todo", "researcher"),), "new"),
            ((issue("TASK-3", "In Progress", "researcher", priority=3),), resume),
        ):
            with self.subTest(out=out):
                self.lines, self.hist = [], {}
                fake = self.fake(
                    issue("TASK-1", "In Progress", "researcher", priority=1, updated=ago(hours=5)),
                    *others,
                )
                self.resumable("TASK-1", "a", 300)
                self.touch("a", 5)
                if any(i["state"] == "In Progress" for i in others):
                    self.resumable("TASK-3", "c", 100)
                self.assertEqual(self.plan(fake), out)
                self.assertEqual(fake.mutations, [])

    def test_live_session_issue_skipped_first_and_every_candidate_returned(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=1, updated=ago(hours=3)),
            issue("TASK-2", "In Progress", "researcher", priority=2, updated=ago(hours=3)),
            issue("TASK-3", "In Progress", "researcher", priority=3),
            issue("TASK-4", "In Progress", "pm", priority=4, project=PD),
        )
        for m in (390, 380, 370, 360):
            self.add("start", "TASK-1", f"a{m}", m)
        self.resumable("TASK-3", "c", 100)
        self.resumable("TASK-4", "d", 200, "pm")
        cands = self.board(fake, lambda b: b.recover({"TASK-1"}))
        self.assertEqual(
            [(i["identifier"], self.unmap(sid)) for i, sid in cands],
            [("TASK-3", "c"), ("TASK-4", "d")],
        )
        self.assertEqual(
            self.said(), [f"recover TASK-2 to=todo reason=no session updated={ago(hours=3)}"]
        )
        self.assertEqual(
            {i: t["state"] for i, t in fake.issues.items()},
            {
                "TASK-1": "In Progress",
                "TASK-2": "Todo",
                "TASK-3": "In Progress",
                "TASK-4": "In Progress",
            },
        )
        self.assertNotIn("comments", fake.issues["TASK-1"])
        self.assertNotIn("TASK-1", [v["i"] for q, v in fake.queries if "history" in q])
        self.assertEqual({self.unmap(sid) for _, sid, _ in self.api.info}, {"c", "d"})

    def test_resume_order(self):
        r = "researcher"
        rows = (
            ((("TASK-1", 3, 300, r, DR), ("TASK-2", 2, 100, r, DR)), DR),
            ((("TASK-1", 2, 100, r, DR), ("TASK-2", 2, 300, r, DR)), DR),
            ((("TASK-1", 0, 300, r, DR), ("TASK-2", 4, 100, r, DR)), DR),
            ((("TASK-1", 2, 120, r, DR), ("TASK-2", 2, 60, "pm", PD)), PD),
        )
        for specs, project in rows:
            with self.subTest(specs=specs):
                self.lines, self.hist = [], {}
                fake = self.fake(
                    *[
                        issue(i, "In Progress", role, priority=p, project=proj)
                        for i, p, _, role, proj in specs
                    ]
                )
                for ident, _, m, role, _ in specs:
                    self.resumable(ident, ident.lower(), m, role)
                self.assertEqual(
                    self.plan(fake), f"resume TASK-2 task-2 https://linear.app/x/TASK-2 {project}"
                )

    def test_no_current_sid_never_outranks(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=1),
            issue("TASK-2", "In Progress", "researcher", priority=4),
        )
        self.add("start", "TASK-1", "old", 400)
        self.touch("old", 300)
        self.moved("TASK-1", 60, actor=USER)
        self.resumable("TASK-2", "b", 100)
        self.assertEqual(
            self.plan(fake), "resume TASK-2 b https://linear.app/x/TASK-2 Deep Research"
        )

    def test_sid_without_start_sorts_by_first_line(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=2),
            issue("TASK-2", "In Progress", "researcher", priority=2),
        )
        self.add("resume", "TASK-1", "a", 300)
        self.add("resume", "TASK-1", "a", 50)
        self.touch("a", 40)
        self.resumable("TASK-2", "b", 200)
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )

    def walk_fixture(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=2),
            issue("TASK-4", "In Progress", "researcher", priority=2),
            issue("TASK-3", "In Progress", "researcher", priority=3),
            issue("TASK-5", "In Progress", "researcher", priority=4),
            issue("TASK-6", "In Progress", "researcher", priority=4),
            issue("TASK-9", "Todo", "researcher", priority=1),
        )
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
        for mode, out in (
            ("plan", resume),
            ("dry plan", resume),
            ("pick", "TASK-9 https://linear.app/x/TASK-9 Deep Research"),
        ):
            with self.subTest(mode=mode):
                self.lines, self.hist = [], {}
                fake = self.walk_fixture()
                self.assertEqual(
                    self.claim(fake, recover=True)
                    if mode == "pick"
                    else self.plan(fake, dry=mode == "dry plan"),
                    out,
                )
                self.assertIn("recover TASK-1 to=todo reason=not resumable sid=a", self.said())
                self.assertIn("recover TASK-5 to=in_review reason=reached 4 attempts", self.said())
                if mode == "dry plan":
                    self.assertEqual(fake.mutations, [])
                    continue
                states = {
                    i: fake.issues[i]["state"]
                    for i in ("TASK-1", "TASK-3", "TASK-4", "TASK-5", "TASK-6")
                }
                self.assertEqual(
                    states,
                    {
                        "TASK-1": "Todo",
                        "TASK-3": "In Progress",
                        "TASK-4": "In Review",
                        "TASK-5": "In Review",
                        "TASK-6": "In Progress",
                    },
                )
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
        self.assertEqual(
            (fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]),
            ("Todo", who("researcher")),
        )

    def test_no_sid_keeps_two_hour_rule(self):
        self.write_config(HUMANS + CONFIG)
        fake = FakeLinear(
            [
                issue("TASK-1", "In Progress", "engineer", updated=ago(hours=3), project=PD),
                issue("TASK-2", "In Progress", "researcher", updated=ago(hours=1)),
            ]
        )
        self.plan(fake)
        t = fake.issues["TASK-1"]
        self.assertEqual(
            (t["state"], t["assignee"], t["comments"]),
            ("Todo", who("engineer"), [router.INTERRUPTED]),
        )
        self.assertNotIn("subscribers", t)
        self.assertFalse(any("assigneeId" in repr(v) for _, v in fake.mutations))
        self.assertEqual(fake.issues["TASK-2"]["state"], "In Progress")

    def test_stale_sid_goes_to_two_hour_rule(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=220)))
        self.add("start", "TASK-1", "old", 400)
        self.touch("old", 400)
        self.moved("TASK-1", 220, actor=USER)
        self.assertEqual(self.plan(fake), "new")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_tolerance(self):
        for start, want in ((100.05, "resume"), (106, "")):
            self.lines, self.hist = [], {}
            fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
            self.moved("TASK-1", 100)
            self.add("start", "TASK-1", "a", start)
            self.touch("a", 40)
            self.assertEqual(self.plan(fake).split(" ")[0], want)

    def test_no_move_in_history_is_current(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.add("start", "TASK-1", "a", 100)
        self.touch("a", 40)
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )

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
                self.assertEqual(
                    self.claim(fake), "TASK-1 https://linear.app/x/TASK-1 Deep Research"
                )
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"][-1], router.CAP_COMMENT)

    def reset_fixture(self):
        self.write_config(HUMANS + CONFIG)
        self.moved("TASK-1", 700)
        self.add("start", "TASK-1", "old", 699)
        for m in (650, 600, 550):
            self.add("resume", "TASK-1", "old", m)
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.resumable("TASK-1", "new", 100)
        self.add("resume", "TASK-1", "new", 60)

    def test_user_reset_resumes_and_orders_by_new_sid(self):
        rows = (
            ("resumes", (), "resume TASK-1 new https://linear.app/x/TASK-1 Deep Research"),
            (
                "orders",
                (issue("TASK-2", "In Progress", "researcher", priority=2),),
                "resume TASK-2 b https://linear.app/x/TASK-2 Deep Research",
            ),
        )
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
                self.write_config(
                    (f"human_members = {json.dumps(members)}\n" if members else "") + CONFIG
                )
                fake = FakeLinear(
                    [issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=40))]
                )
                self.lines = []
                for i, sid in enumerate("abcd"):
                    self.add("start", "TASK-1", sid, 400 - i * 50)
                self.assertEqual(self.plan(fake), "")
                t = fake.issues["TASK-1"]
                self.assertEqual(
                    (t["state"], t["assignee"], t["comments"], t.get("subscribers", [])),
                    ("In Review", who("researcher"), [router.CAP_COMMENT], members),
                )
                self.assertEqual(
                    writes(fake),
                    [("issueSubscribe", "TASK-1")] * len(members)
                    + [("read", "TASK-1"), ("issueUpdate", "TASK-1"), ("commentCreate", "TASK-1")],
                )
                self.assertEqual(fake.mutations[-2][1]["s"], STATES["In Review"])

    def test_unknown_human_fails_loud(self):
        self.write_config('human_members = ["nobody@x.com"]\n' + CONFIG)
        with self.assertRaises(SystemExit) as e:
            self.plan(FakeLinear([]))
        self.assertIn("nobody@x.com", str(e.exception))

    def capped(self, hist):
        self.write_config(HUMANS + CONFIG)
        fake = FakeLinear(
            [issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))], {"TASK-1": hist}
        )
        for i, sid in enumerate("abcd"):
            self.add("start", "TASK-1", sid, 400 - i * 50)
        self.touch("d", 200)
        return fake, self.plan(fake)

    def test_attempt_cap_reset_only_by_a_human_members_move_to_todo(self):
        def move(m, actor, state="Todo"):
            return {"createdAt": ago(minutes=m), "actorId": actor, "toStateId": STATES[state]}

        rows = (
            (
                "user",
                [move(380, USER)],
                "resume TASK-1 d https://linear.app/x/TASK-1 Deep Research",
                "In Progress",
            ),
            (
                "not a human member",
                [
                    move(380, AGENT),
                    move(375, "role-account"),
                    move(370, USER, "In Progress"),
                    move(360, None),
                ],
                "",
                "In Review",
            ),
        )
        for name, hist, out, state in rows:
            with self.subTest(name):
                self.lines = []
                fake, got = self.capped(hist)
                self.assertEqual((got, fake.issues["TASK-1"]["state"]), (out, state))
                if state == "In Progress":
                    self.assertEqual(fake.mutations, [])

    def test_skipped_move_posts_no_comment(self):
        fake = Moved(
            [issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))], "In Review"
        )
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


class Recover(Base):
    """FR-47: liveness and resumability are the Driver's session status; what it cannot say changes nothing."""

    def test_a_session_active_within_live_is_not_resumed(self):
        for minutes, out in (
            (10, ""),
            (29, ""),
            (31, "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"),
        ):
            with self.subTest(minutes=minutes):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
                self.resumable("TASK-1", "a", 60)
                self.touch("a", minutes)
                self.assertEqual(self.plan(fake), out)
                self.assertEqual((fake.mutations, self.said()), ([], []))

    def test_not_resumable_goes_to_todo_after_live(self):
        for minutes, out, state in ((20, "", "In Progress"), (40, "new", "Todo")):
            with self.subTest(minutes=minutes):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
                self.moved("TASK-1", minutes + 1)
                self.add("start", "TASK-1", "a", minutes)
                self.api.statuses[self.sid("a")] = api.SessionStatus("no", None, "")
                self.assertEqual(self.plan(fake), out)
                t = fake.issues["TASK-1"]
                self.assertEqual(
                    (t["state"], t.get("comments")), (state, [router.INTERRUPTED] if out else None)
                )
                self.assertEqual(
                    self.said(),
                    ["recover TASK-1 to=todo reason=not resumable sid=a"] if out else [],
                )

    def test_an_unknown_session_changes_nothing_and_is_said_once_a_day(self):
        statuses = (
            api.SessionStatus("unknown", None, ""),
            api.DriverError("boom"),
            api.ConfigError("no runtime a"),
            OSError("gone"),
        )
        for st in statuses:
            with self.subTest(st=st):
                self.lines, self.hist = [], {}
                fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
                self.resumable("TASK-1", "a", 300)
                self.api.statuses[self.sid("a")] = st
                for again, said in (
                    (False, ["recover-skip TASK-1 sid=a reason=session unknown"]),
                    (True, []),
                ):
                    self.assertEqual(self.plan(fake, again=again), "")
                    self.assertEqual(self.said(), said)
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], fake.mutations), ("In Progress", [])
                )
                with open(self.runs) as f:
                    self.assertEqual(f.read(), "".join(f"{line}\n" for line in self.lines))

    def test_an_unknown_session_is_not_settled_without_the_wait(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)))
        self.resumable("TASK-1", "a", 300)
        self.api.statuses[self.sid("a")] = api.DriverError("boom")

        def use(b):
            (found,) = b.issues("in_progress")
            return b.settle(found, b.current_sid(found), wait=False)

        self.assertFalse(self.board(fake, use))
        self.assertEqual(self.said(), ["recover-skip TASK-1 sid=a reason=session unknown"])
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("In Progress", []))

    def test_one_status_read_per_sid_with_its_recorded_runtime_at_the_workdir(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.resumable("TASK-1", "a", 60)
        self.write_config(CONFIG.replace(OVERLAY, '[core]\nruntime = "b"\n'))
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )
        self.assertEqual(
            self.api.info, [("a", self.sid("a"), os.path.join(self.work, "work", "TASK-1"))]
        )


class Claim(Base):
    def test_claim_order(self):
        rows = (
            (
                [
                    issue(
                        "TASK-1", "Todo", "researcher", priority=0, created="2026-01-01T00:00:00Z"
                    ),
                    issue(
                        "TASK-2", "Todo", "researcher", priority=3, created="2026-02-01T00:00:00Z"
                    ),
                    issue(
                        "TASK-3", "Todo", "researcher", priority=3, created="2026-01-15T00:00:00Z"
                    ),
                ],
                ["TASK-3", "TASK-2", "TASK-1"],
            ),
            (
                [
                    issue(
                        "TASK-1", "Todo", "researcher", priority=2, created="2026-09-01T00:00:00Z"
                    ),
                    issue(
                        "TASK-2",
                        "Todo",
                        "pm",
                        priority=2,
                        created="2026-09-02T00:00:00Z",
                        project=PD,
                    ),
                    issue(
                        "TASK-3", "Todo", "researcher", priority=1, created="2026-09-03T00:00:00Z"
                    ),
                ],
                ["TASK-3", "TASK-2", "TASK-1"],
            ),
            (
                [
                    issue(
                        "TASK-1", "Todo", "researcher", priority=2, created="2026-09-01T00:00:00Z"
                    ),
                    issue("TASK-2", "Todo", "engineer", priority=2, created="2026-09-03T00:00:00Z"),
                    issue("TASK-3", "Todo", "pm", priority=2, created="2026-09-02T00:00:00Z"),
                    issue("TASK-4", "Todo", "engineer", priority=2, created="2026-09-02T00:00:00Z"),
                ],
                ["TASK-4", "TASK-2", "TASK-3", "TASK-1"],
            ),
        )
        for row, (listed, order) in enumerate(rows):
            with self.subTest(row=row):
                fake = FakeLinear(listed)
                self.assertEqual([self.claim(fake).split()[0] for _ in listed], order)
                self.assertEqual({i["state"] for i in fake.issues.values()}, {"In Progress"})

    def test_queue_only_role_accounts_across_projects(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo"),
                issue("TASK-2", "Todo", "someone"),
                issue("TASK-3", "Todo", "pm", project=None),
                issue("TASK-4", "Todo", "engineer", project=PD, priority=3),
            ]
        )
        self.assertEqual(self.claim(fake), f"TASK-4 https://linear.app/x/TASK-4 {PD}")
        self.assertEqual(
            [i for i in ("TASK-1", "TASK-2", "TASK-3") if fake.issues[i]["state"] != "Todo"], []
        )
        flt = next(v["f"] for q, v in fake.queries if "issues(filter" in q)
        self.assertEqual(
            (flt["team"], flt["project"], sorted(flt["assignee"]["id"]["in"])),
            ({"id": {"eq": TEAM}}, {"null": False}, sorted(f"u-{r}" for r in ROLE)),
        )

    def test_claim_only_moves_to_in_progress(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "pm")])
        self.claim(fake)
        ((q, v),) = fake.mutations
        self.assertNotIn("assignee", q + repr(v))
        self.assertEqual(
            (fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]),
            ("In Progress", who("pm")),
        )
        self.assertEqual(self.taken[1:], (None, "pm", "b"))

    def test_full_roles_not_picked(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "engineer", priority=1),
                issue("TASK-2", "Todo", "researcher", priority=4),
            ]
        )
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(
            self.claim(fake, full={"engineer"}), "TASK-2 https://linear.app/x/TASK-2 Deep Research"
        )
        self.assertEqual(
            self.said(), ["pick TASK-2 queue=1", "claim TASK-2 role=researcher runtime=a"]
        )
        self.assertEqual(
            (fake.issues["TASK-1"]["state"], "comments" in fake.issues["TASK-1"]), ("Todo", False)
        )
        flt = next(v["f"] for q, v in fake.queries if "issues(filter" in q)
        self.assertEqual(sorted(flt["assignee"]["id"]["in"]), sorted(f"u-{r}" for r in ROLE))

    def test_capped_todo_goes_to_review(self):
        self.write_config(HUMANS + CONFIG)
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "researcher", priority=2),
            ]
        )
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.CAP_COMMENT])
        self.assertEqual(
            (fake.issues["TASK-1"]["assignee"], fake.issues["TASK-1"]["subscribers"]),
            (who("researcher"), ["me@x.com"]),
        )

    def test_capped_todo_reset_by_user(self):
        self.write_config(HUMANS + CONFIG)
        hist = {
            "TASK-1": [
                {"createdAt": ago(minutes=200), "actorId": USER, "toStateId": STATES["Todo"]}
            ]
        }
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")], hist)
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "TASK-1 https://linear.app/x/TASK-1 Deep Research")

    def test_no_longer_todo_skipped_for_the_next(self):
        class Snatched(FakeLinear):
            def __call__(self, query, **v):
                out = super().__call__(query, **v)
                if "issues(filter" in query and "inverseRelations" in query:
                    self.issues["TASK-1"]["state"] = "In Progress"
                return out

        fake = Snatched(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "researcher", priority=2),
            ]
        )
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=2",
                "claim-skip TASK-1 reason=no longer Todo",
                "pick TASK-2 queue=2",
                "claim TASK-2 role=researcher runtime=a",
            ],
        )
        self.assertEqual([v["i"] for _, v in fake.mutations], ["TASK-2"])

    def test_skipped_cap_move_posts_no_comment(self):
        self.write_config(HUMANS + CONFIG)
        fake = Moved([issue("TASK-1", "Todo", "researcher")], "In Progress")
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "")
        t = fake.issues["TASK-1"]
        self.assertEqual((t["state"], t["subscribers"]), ("In Progress", ["me@x.com"]))
        self.assertNotIn("comments", t)
        self.assertEqual(writes(fake), [("issueSubscribe", "TASK-1"), ("read", "TASK-1")])
        self.assertEqual(
            self.said(),
            [
                "claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                f"move-skip TASK-1 step=pick state={STATES['In Progress']}",
                "pick-none reason=nothing claimable",
            ],
        )

    def test_refused_claim_raises(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        fake.refuse.add("TASK-1")
        with self.assertRaises(RuntimeError) as cm:
            self.claim(fake)
        self.assertEqual(str(cm.exception), "issueUpdate: success: false")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")


class Recheck(Base):
    """FR-56: the claim re-reads assignee and project; the claim is the recheck's."""

    def test_reassigned_to_no_role_account_is_not_claimed(self):
        for assignee in ({"id": USER, "email": "me@x.com"}, who("someone"), None):
            with self.subTest(assignee=assignee):
                fake = Changed(
                    [
                        issue("TASK-1", "Todo", "researcher", priority=1),
                        issue("TASK-2", "Todo", "researcher", priority=2),
                    ],
                    {"TASK-1": {"assignee": assignee}},
                )
                self.assertEqual(
                    self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research"
                )
                self.assertEqual(
                    self.said(),
                    [
                        "pick TASK-1 queue=2",
                        "claim-skip TASK-1 reason=not assigned to a role",
                        "pick TASK-2 queue=2",
                        "claim TASK-2 role=researcher runtime=a",
                    ],
                )
                self.assertEqual([v["i"] for _, v in fake.mutations], ["TASK-2"])

    def test_reassigned_to_another_role_is_claimed_as_that_role(self):
        fake = Changed([issue("TASK-1", "Todo", "researcher")], {"TASK-1": {"assignee": who("pm")}})
        self.assertEqual(
            self.claim(fake, gate=True), "TASK-1 https://linear.app/x/TASK-1 Deep Research"
        )
        taken, task, role, runtime = self.taken
        self.assertEqual((taken["assignee"], task, role, runtime), (who("pm"), None, "pm", "b"))
        self.assertEqual(self.said(), ["pick TASK-1 queue=1", "claim TASK-1 role=pm runtime=b"])
        self.assertEqual(self.api.usage_calls, ["b"])
        a = router.begin("new", taken, "s1", role, task, runtime, self.runs)
        self.assertEqual((a.assignee, a.runtime), (ROLE["pm"], "b"))

    def test_the_task_label_is_checked_against_the_new_role(self):
        self.write_config(HUMANS + CONFIG + TASK_LABELS)
        fake = Changed(
            [issue("TASK-1", "Todo", "researcher", labels=[label("Light Research", LIGHT)])],
            {"TASK-1": {"assignee": who("pm")}},
        )
        self.assertEqual(self.claim(fake), "")
        t = fake.issues["TASK-1"]
        self.assertEqual(
            (t["state"], t["comments"]),
            (
                "In Review",
                [
                    'Task label "Light Research" is not one of pm\'s tasks (product-design). '
                    "Fix the label or the assignee, then move the issue back to Todo."
                ],
            ),
        )

    def test_a_full_new_role_is_skipped(self):
        fake = Changed([issue("TASK-1", "Todo", "researcher")], {"TASK-1": {"assignee": who("pm")}})
        self.assertEqual(self.claim(fake, full={"pm"}), "")
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=1",
                "claim-skip TASK-1 reason=pm is full",
                "pick-none reason=nothing claimable",
            ],
        )
        self.assertEqual((fake.mutations, fake.issues["TASK-1"]["state"]), ([], "Todo"))

    def test_a_changed_project_is_the_runs(self):
        fake = Changed(
            [issue("TASK-1", "Todo", "researcher")],
            {"TASK-1": {"project": {"id": IDS[PD], "name": PD}}},
        )
        self.assertEqual(self.claim(fake), f"TASK-1 https://linear.app/x/TASK-1 {PD}")
        a = router.begin("new", self.taken[0], "s1", "researcher", None, "a", self.runs)
        self.assertEqual(a.project, IDS[PD])

    def test_a_removed_project_is_skipped(self):
        fake = Changed([issue("TASK-1", "Todo", "researcher")], {"TASK-1": {"project": None}})
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=1",
                "claim-skip TASK-1 reason=no project",
                "pick-none reason=nothing claimable",
            ],
        )
        self.assertEqual((fake.mutations, fake.issues["TASK-1"]["state"]), ([], "Todo"))


class Blockers(Base):
    def test_blocked_skipped_for_next_ready(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    priority=1,
                    inverse=[
                        blocker("TASK-7"),
                        blocker("TASK-8", "completed"),
                        blocker("TASK-9", "unstarted"),
                    ],
                ),
                issue("TASK-2", "Todo", "researcher", priority=2),
                issue("TASK-3", "Todo", "researcher", priority=3),
            ]
        )
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        self.assertEqual(
            self.said(),
            [
                'blocked TASK-1 by=["TASK-7", "TASK-9"]',
                "pick TASK-2 queue=2",
                "claim TASK-2 role=researcher runtime=a",
            ],
        )
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_done_blockers(self):
        for states, blocked in (
            (["completed"], False),
            (["canceled"], False),
            (["duplicate"], False),
            (["completed", "started"], True),
        ):
            fake = FakeLinear(
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        inverse=[blocker(f"TASK-{n}", s) for n, s in enumerate(states, 7)],
                    )
                ]
            )
            self.claim(fake)
            self.assertEqual(
                fake.issues["TASK-1"]["state"], "Todo" if blocked else "In Progress", states
            )
            self.assertEqual(
                [m for m in self.said() if m.startswith("blocked ")],
                ['blocked TASK-1 by=["TASK-8"]'] if blocked else [],
                states,
            )

    def test_capped_blocked_stays_todo(self):
        self.write_config(HUMANS + CONFIG)
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")])])
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(
            self.said(), ['blocked TASK-1 by=["TASK-7"]', "pick-none reason=queue empty"]
        )
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("Todo", []))
        self.assertNotIn("comments", fake.issues["TASK-1"])

    def test_only_inverse_blocks_block(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    priority=2,
                    inverse=[
                        blocker("TASK-5", kind="related"),
                        blocker("TASK-6", kind="duplicate"),
                    ],
                ),
                issue(
                    "TASK-2",
                    "Todo",
                    "researcher",
                    priority=1,
                    inverse=[blocker("TASK-1", "unstarted")],
                ),
            ]
        )
        self.assertEqual(self.claim(fake).split()[0], "TASK-1")
        self.assertEqual(
            self.said(),
            [
                'blocked TASK-2 by=["TASK-1"]',
                "pick TASK-1 queue=1",
                "claim TASK-1 role=researcher runtime=a",
            ],
        )
        self.assertFalse(any(re.search(r"\brelations\b", q) for q, _ in fake.queries))

    def test_null_blocker_unreadable(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    inverse=[blocker(None), blocker("TASK-7", "completed")],
                )
            ]
        )
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(
            self.said(), ['blocked TASK-1 by=["(unreadable)"]', "pick-none reason=queue empty"]
        )

    def test_blocker_query_error_reads_per_issue(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "researcher", priority=2, inverse=[blocker("TASK-7")]),
                issue(
                    "TASK-3",
                    "Todo",
                    "researcher",
                    priority=3,
                    inverse=[blocker("TASK-8", "canceled")],
                ),
            ]
        )
        fake.todo_error, fake.unreadable = True, {"TASK-1"}
        self.assertEqual(self.claim(fake), "TASK-3 https://linear.app/x/TASK-3 Deep Research")
        said = self.said()
        self.assertRegex(said[0], r"^todo-error error=SystemExit: linear api error: ")
        self.assertEqual(
            said[1:],
            [
                'blocked TASK-1 by=["(unreadable)"]',
                'blocked TASK-2 by=["TASK-7"]',
                "pick TASK-3 queue=1",
                "claim TASK-3 role=researcher runtime=a",
            ],
        )
        self.assertEqual(fake.reads(), [router.q_issues(router.RELATIONS), router.q_issues()])
        self.assertEqual(
            [v["i"] for q, v in fake.queries if "issue(id:" in q and "inverseRelations" in q],
            ["TASK-1", "TASK-2", "TASK-3"],
        )

    def test_one_todo_read_with_relations(self):
        for argv in ("claim", "plan", "pick"):
            fake = FakeLinear(
                [
                    issue("TASK-1", "In Progress", "researcher"),
                    issue("TASK-2", "Todo", "researcher"),
                ]
            )
            self.plan(fake) if argv == "plan" else self.claim(fake, recover=argv == "pick")
            reads = {s: fake.reads(s) for s in ("Todo", "In Progress")}
            self.assertEqual(len(reads["Todo"]), 1, argv)
            self.assertIn(
                "inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }",
                reads["Todo"][0],
                argv,
            )
            self.assertEqual(len(reads["In Progress"]), 0 if argv == "claim" else 1, argv)
            self.assertFalse(any("inverseRelations" in q for q in reads["In Progress"]), argv)
            self.assertFalse(
                any("issue(id:" in q and "inverseRelations" in q for q, _ in fake.queries), argv
            )

    def test_dry_run_logs_blocked(self):
        for argv, out, last in (
            ("claim", f"TASK-2 https://linear.app/x/TASK-2 {PD}", ["pick TASK-2 queue=1"]),
            ("plan", "new", []),
        ):
            fake = FakeLinear(
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        inverse=[blocker("TASK-7"), blocker("TASK-8", "backlog")],
                    ),
                    issue("TASK-2", "Todo", "pm", project=PD),
                ]
            )
            self.assertEqual(
                self.plan(fake, dry=True) if argv == "plan" else self.claim(fake, dry=True),
                out,
                argv,
            )
            self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7", "TASK-8"]', *last], argv)
            self.assertEqual(fake.mutations, [], argv)


class TaskLabels(Base):
    ORPHAN = (
        'Task label "Orphan" is not one of researcher\'s tasks (deep-research, light-research). '
        "Fix the label or the assignee, then move the issue back to Todo."
    )

    def setUp(self):
        super().setUp()
        self.write_config(HUMANS + CONFIG + TASK_LABELS)

    def test_task_for(self):
        outside = [
            label("Urgent", STRAY, None),
            label("Light Research", LIGHT, "00000000-0000-4000-8000-000000000003"),
        ]
        role_tasks = ["deep-research", "light-research"]
        label_tasks = {
            LIGHT: "light-research",
            DEEP: "deep-research",
            ORPHAN_LABEL: "product-design",
        }
        fix = "Fix the label or the assignee, then move the issue back to Todo."
        absent = "is not in ~/.agent-pm/orchestrator-new.local.toml's [task_labels]. Fix the label, then move the issue back to Todo."
        cases = (
            ([], (None, None)),
            (outside, (None, None)),
            ([label("Light Research", LIGHT)], ("light-research", None)),
            ([label("Renamed in Linear", LIGHT)], ("light-research", None)),
            (outside + [label("Light Research", LIGHT)], ("light-research", None)),
            ([label("Whatever", DEEP)], ("deep-research", None)),
            ([label("Light Research", STRAY)], (None, f'Task label "Light Research" {absent}')),
            ([label("light-research", STRAY)], (None, f'Task label "light-research" {absent}')),
            (
                [label("Orphan", ORPHAN_LABEL)],
                (
                    None,
                    f'Task label "Orphan" is not one of researcher\'s tasks (deep-research, light-research). {fix}',
                ),
            ),
            (
                [label("Light Research", LIGHT), outside[0], label("Deep Research", DEEP)],
                (
                    None,
                    "Several task labels (Light Research, Deep Research); keep one, then move the issue back to Todo.",
                ),
            ),
        )
        for labels, want in cases:
            self.assertEqual(
                router.task_for(labels, TASK_GROUP, "researcher", role_tasks, label_tasks),
                want,
                labels,
            )

    def test_fr7_invalid_label_goes_to_review(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    priority=1,
                    labels=[label("Orphan", ORPHAN_LABEL)],
                ),
                issue("TASK-2", "Todo", "researcher", priority=2),
            ]
        )
        self.assertEqual(self.claim(fake), "TASK-2 https://linear.app/x/TASK-2 Deep Research")
        t = fake.issues["TASK-1"]
        self.assertEqual(
            (t["state"], t["assignee"], t["comments"], t["subscribers"]),
            ("In Review", who("researcher"), [self.ORPHAN], ["me@x.com"]),
        )
        self.assertEqual(
            writes(fake),
            [
                ("issueSubscribe", "TASK-1"),
                ("read", "TASK-1"),
                ("issueUpdate", "TASK-1"),
                ("commentCreate", "TASK-1"),
                ("issueUpdate", "TASK-2"),
            ],
        )
        self.assertEqual(fake.mutations[1][1]["s"], STATES["In Review"])
        self.assertIn("claim-skip TASK-1 to=in_review reason=bad task label", self.said())

    def test_capped_and_bad_label_todo_skipped_for_the_next(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue(
                    "TASK-2",
                    "Todo",
                    "researcher",
                    priority=2,
                    labels=[label("Orphan", ORPHAN_LABEL)],
                ),
                issue("TASK-3", "Todo", "pm", priority=3, project=PD),
                issue("TASK-4", "Todo", "engineer", priority=4, project=PD),
            ]
        )
        for sid in "abcd":
            self.add("start", "TASK-1", sid, 300)
        self.assertEqual(self.claim(fake), f"TASK-3 https://linear.app/x/TASK-3 {PD}")
        self.assertEqual(
            self.said(),
            [
                "claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                "pick TASK-2 queue=4",
                "claim-skip TASK-2 to=in_review reason=bad task label",
                "pick TASK-3 queue=4",
                "claim TASK-3 role=pm runtime=b",
            ],
        )
        self.assertEqual(
            {i: fake.issues[i].get("comments") for i in ("TASK-1", "TASK-2")},
            {"TASK-1": [router.CAP_COMMENT], "TASK-2": [self.ORPHAN]},
        )
        self.assertEqual([i for op, i in writes(fake) if op == "read"], ["TASK-1", "TASK-2"])
        self.assertEqual([v["i"] for q, v in fake.queries if q == RECHECK], ["TASK-2", "TASK-3"])
        self.assertEqual(fake.issues["TASK-4"]["state"], "Todo")

    def test_blocked_invalid_label_waits_for_its_blocker(self):
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    labels=[label("Orphan", ORPHAN_LABEL)],
                    inverse=[blocker("TASK-7")],
                )
            ]
        )
        self.assertEqual(self.claim(fake), "")
        self.assertEqual(
            self.said(), ['blocked TASK-1 by=["TASK-7"]', "pick-none reason=queue empty"]
        )
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("Todo", []))
        self.assertNotIn(RECHECK, [q for q, _ in fake.queries])
        fake.issues["TASK-1"]["inverseRelations"]["nodes"][0]["issue"]["state"]["type"] = (
            "completed"
        )
        self.assertEqual(self.claim(fake), "")
        t = fake.issues["TASK-1"]
        self.assertEqual((t["state"], t["comments"]), ("In Review", [self.ORPHAN]))
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=1",
                "claim-skip TASK-1 to=in_review reason=bad task label",
                "pick-none reason=nothing claimable",
            ],
        )

    def test_fr4_label_read_from_recheck(self):
        class Relabel(FakeLinear):
            def __call__(self, query, **v):
                out = super().__call__(query, **v)
                if "issues(filter" in query:
                    self.issues["TASK-1"]["labels"] = [label("Light Research", LIGHT)]
                return out

        fake = Relabel([issue("TASK-1", "Todo", "researcher")])
        self.claim(fake)
        self.assertIn("claim TASK-1 role=researcher task=light-research runtime=a", self.said())
        self.assertEqual(self.taken[1:], ("light-research", "researcher", "a"))
        self.assertNotIn("labels", fake.reads()[0])

    def test_fr8_dry_run_resolves_nothing(self):
        for argv in (False, True):
            fake = FakeLinear(
                [issue("TASK-1", "Todo", "researcher", labels=[label("Orphan", ORPHAN_LABEL)])]
            )
            self.assertEqual(
                self.claim(fake, dry=True, recover=argv),
                "TASK-1 https://linear.app/x/TASK-1 Deep Research",
                argv,
            )
            self.assertEqual(self.said(), ["pick TASK-1 queue=1"], argv)
            self.assertEqual(
                ([q for q, _ in fake.queries if q == RECHECK], fake.mutations), ([], []), argv
            )
            self.assertEqual(self.taken[1:], (None, "researcher", "a"))

    def test_nfr1_claim_reads_one_query_per_issue(self):
        fake = FakeLinear(
            [issue("TASK-1", "Todo", "researcher", labels=[label("Light Research", LIGHT)])]
        )
        self.claim(fake)
        self.assertEqual(
            [(q, v) for q, v in fake.queries if "issue(id:" in q], [(RECHECK, {"i": "TASK-1"})]
        )
        todo = [q for q, _ in fake.queries].index(fake.reads()[0])
        self.assertEqual([q for q, _ in fake.queries[todo + 1 :]], [RECHECK, fake.mutations[0][0]])

    def test_fr22_every_resumable_issue_is_checked(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "In Progress", "researcher", priority=1),
                issue("TASK-2", "In Progress", "researcher", priority=2),
            ],
            self.hist,
        )
        self.resumable("TASK-1", "a", 60)
        self.resumable("TASK-2", "b", 60, "pm")
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )
        self.assertEqual(
            (fake.issues["TASK-1"]["state"], fake.issues["TASK-2"]["state"]),
            ("In Progress", "In Review"),
        )
        self.assertIn(
            "recover TASK-2 to=in_review reason=role (pm) is not the assignee's (researcher)",
            self.said(),
        )


class Gate(Base):
    """The usage gate's thresholds over the Driver's Usage (the old rate_limit_event rows)."""

    def gate(self):
        self.write_runs()
        return router.Gate(self.runs, NOW, False)

    def test_thresholds(self):
        self.assertEqual((router.MAX_5H, router.BRAKE_5H, router.MAX_WEEK), (90, 80, 100))
        rows = (
            (reading(89.9, 99.9), None),
            (reading(90, 10), "reason=usage usage=five_hour=90% weekly=10%"),
            (reading(10, 100), "reason=usage usage=five_hour=10% weekly=100%"),
            (reading(150, None), "reason=usage usage=five_hour=150% weekly=none"),
            (reading(None, 99), None),
            (reading(89, None), None),
            (reading(None, None), None),
            (None, "reason=usage unknown"),
            (api.ConfigError("no runtime a"), "reason=usage unknown"),
            (RuntimeError("boom"), "reason=usage unknown"),
        )
        for value, why in rows:
            with self.subTest(value=value):
                self.api.usages = {"a": value}
                with redirect_stderr(io.StringIO()), self.log_dir(False):
                    self.assertEqual(self.gate().blocked("a", "TASK-1"), why is not None)
                self.raw = ""
                self.assertEqual(self.said(), [f"usage-skip TASK-1 runtime=a {why}"] if why else [])

    def test_one_probe_per_runtime(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        with redirect_stderr(io.StringIO()), self.log_dir(False):
            gate = self.gate()
            got = [
                gate.blocked(r, i) for r, i in (("a", "TASK-1"), ("b", "TASK-2"), ("a", "TASK-3"))
            ]
        self.assertEqual((got, self.api.usage_calls), ([True, False, True], ["a", "b"]))

    def test_summary(self):
        rows = (
            (None, "unknown"),
            (reading(95, 10.5), "five_hour=95% weekly=10.5%"),
            (reading(None, 100), "five_hour=none weekly=100%"),
            (reading(89.94, None), "five_hour=89.94% weekly=none"),
        )
        for usage, out in rows:
            self.assertEqual(router.summary(usage), out)


class UsageGate(Base):
    """FR-48: each candidate gates on its own runtime's usage."""

    def test_a_blocked_head_runtime_lets_the_next_runtime_claim(self):
        rows = (
            (reading(95, 10), "reason=usage usage=five_hour=95% weekly=10%"),
            (reading(10, 100), "reason=usage usage=five_hour=10% weekly=100%"),
            (None, "reason=usage unknown"),
            (api.ConfigError("no runtime a"), "reason=usage unknown"),
        )
        for value, why in rows:
            with self.subTest(why=why):
                self.api.usages, self.api.usage_calls = {"a": value, "b": reading(50)}, []
                fake = two()
                self.assertEqual(
                    self.claim(fake, gate=True), f"TASK-2 https://linear.app/x/TASK-2 {PD}"
                )
                self.assertEqual(
                    self.said(),
                    [
                        "pick TASK-1 queue=2",
                        f"usage-skip TASK-1 runtime=a {why}",
                        "pick TASK-2 queue=2",
                        "claim TASK-2 role=pm runtime=b",
                    ],
                )
                self.assertEqual([v["i"] for _, v in fake.mutations], ["TASK-2"])
                self.assertEqual(self.api.usage_calls, ["a", "b"])

    def test_an_absent_window_never_blocks(self):
        self.api.usages = {"a": reading(None, 10)}
        self.assertEqual(
            self.claim(two(), gate=True), "TASK-1 https://linear.app/x/TASK-1 Deep Research"
        )

    def test_two_candidates_of_a_runtime_probe_it_once(self):
        self.api.usages = {"a": reading(95)}
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-3", "Todo", "researcher", priority=2),
                issue("TASK-2", "Todo", "pm", priority=3, project=PD),
            ]
        )
        self.assertEqual(self.claim(fake, gate=True), f"TASK-2 https://linear.app/x/TASK-2 {PD}")
        why = "runtime=a reason=usage usage=five_hour=95% weekly=10%"
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=3",
                f"usage-skip TASK-1 {why}",
                "pick TASK-3 queue=3",
                f"usage-skip TASK-3 {why}",
                "pick TASK-2 queue=3",
                "claim TASK-2 role=pm runtime=b",
            ],
        )
        self.assertEqual(self.api.usage_calls, ["a", "b"])

    def test_all_blocked_claims_nothing(self):
        self.api.usages = {"a": reading(95), "b": None}
        fake = two()
        self.assertEqual(self.claim(fake, gate=True), "")
        self.assertEqual(fake.mutations, [])
        self.assertEqual(self.said()[-1], "pick-none reason=nothing claimable")

    def test_the_claim_gates_on_the_rechecked_roles_runtime(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        rows = (
            ("researcher", "pm", f"TASK-1 https://linear.app/x/TASK-1 {DR}", ["b"]),
            ("pm", "researcher", "", ["a"]),
        )
        for listed, now, out, calls in rows:
            with self.subTest(listed=listed):
                self.api.usage_calls = []
                fake = Changed(
                    [issue("TASK-1", "Todo", listed)], {"TASK-1": {"assignee": who(now)}}
                )
                self.assertEqual(self.claim(fake, gate=True), out)
                self.assertEqual(self.api.usage_calls, calls)
                self.assertEqual(len(fake.mutations), int(bool(out)))

    def test_a_usage_skip_is_said_once_a_day(self):
        self.api.usages = {"a": reading(95)}
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        skip = "usage-skip TASK-1 runtime=a reason=usage usage=five_hour=95% weekly=10%"
        for again, said in ((False, [skip]), (True, [])):
            self.assertEqual(self.claim(fake, gate=True, again=again), "")
            self.assertEqual(
                self.said(), ["pick TASK-1 queue=1", *said, "pick-none reason=nothing claimable"]
            )
        self.assertEqual(self.api.usage_calls, ["a", "a"])

    def test_dry_run_prints_the_usage_of_each_runtime(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        self.limit("a", at)
        fake = two()
        self.assertEqual(
            self.claim(fake, dry=True, gate=True), f"TASK-2 https://linear.app/x/TASK-2 {PD}"
        )
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=2",
                f"usage runtime=a until={at} allowed=false",
                f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                "pick TASK-2 queue=2",
                "usage runtime=b usage=five_hour=10% weekly=10% allowed=true",
            ],
        )
        self.assertEqual(self.taken[1:], (None, "pm", "b"))
        self.assertEqual((fake.mutations, self.api.usage_calls), ([], ["b"]))
        self.assertNotIn(RECHECK, [q for q, _ in fake.queries])


class Limits(Base):
    """FR-60: a live limit row blocks its runtime with no probe; any other limit row changes nothing."""

    def test_a_live_limit_row_skips_its_runtime_without_a_probe(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        self.limit("a", at)
        fake = two()
        self.assertEqual(self.claim(fake, gate=True), f"TASK-2 https://linear.app/x/TASK-2 {PD}")
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=2",
                f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                "pick TASK-2 queue=2",
                "claim TASK-2 role=pm runtime=b",
            ],
        )
        self.assertEqual(self.api.usage_calls, ["b"])

    def test_a_limit_row_not_live_changes_nothing(self):
        rows = (
            (NOW - timedelta(minutes=1)).isoformat(),
            None,
            (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat(),
            5,
            "soon",
            (NOW + timedelta(days=8)).isoformat(),
        )
        for resets_at in rows:
            with self.subTest(resets_at=resets_at):
                self.lines, self.api.usage_calls = [], []
                self.limit("a", resets_at)
                fake = two()
                self.assertEqual(
                    self.claim(fake, gate=True), "TASK-1 https://linear.app/x/TASK-1 Deep Research"
                )
                self.assertEqual(self.api.usage_calls, ["a"])

    def test_limit_rows_are_no_attempts(self):
        for sid in "abc":
            self.add("start", "TASK-1", sid, 300)
        for _ in range(4):
            self.limit("b", (NOW + timedelta(hours=1)).isoformat(), ident="TASK-1")
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.claim(fake), "TASK-1 https://linear.app/x/TASK-1 Deep Research")


class LiveSessions(Base):
    def test_exact_names_of_the_roles(self):
        self.api.live = [
            "agent-pm-engineer-TASK-2",
            "agent-pm-engineer-TASK-10",
            "agent-pm-researcher-TASK-1",
            "agent-pm-pm-TASK-6",
            "agent-pm-pm-lead-TASK-4",
            "agent-pm-engineer",
            "agent-pm-engineer-TASK-1x",
            "agent-pm-engineer-task-3",
            "agent-pm-engineer-TASK-",
            "agent-pm-ghost-TASK-5",
            "engineer-TASK-7-0b6f2c1e",
            "other",
        ]
        self.assertEqual(
            router.live_sessions(["researcher", "pm", "pm-lead", "engineer"]),
            {
                "researcher": ["TASK-1"],
                "pm": ["TASK-6"],
                "pm-lead": ["TASK-4"],
                "engineer": ["TASK-10", "TASK-2"],
            },
        )
        self.assertEqual(self.api.order, [("sessions", "agent-pm-")])

    def test_unknown_sessions_are_none(self):
        self.api.live = None
        self.assertIsNone(router.live_sessions(["researcher", "engineer"]))


class Begin(Base):
    """FR-46: the start or resume line records the runtime; the launch Namespace carries it."""

    def test_one_json_line_and_the_launch_namespace(self):
        path = os.path.join(self.tmp, "logs", "runs.jsonl")
        found = issue("TASK-1", "In Progress", "researcher")
        with mock.patch.object(linear, "stamp", return_value="2026-10-09T00:26:33-04:00"):
            got = [
                router.begin(mode, found, "s1", "researcher", task, "a", path)
                for mode, task in (("new", "light-research"), ("resume", None))
            ]
        with open(path) as f:
            self.assertEqual(
                f.read(),
                "".join(
                    f'{{"ts": "2026-10-09T00:26:33-04:00", "kind": "{kind}", "issue": "TASK-1", '
                    '"sid": "s1", "role": "researcher", "runtime": "a"}\n'
                    for kind in ("start", "resume")
                ),
            )
        self.assertEqual(
            got,
            [
                argparse.Namespace(
                    issue="TASK-1",
                    project=IDS[DR],
                    assignee=ROLE["researcher"],
                    sid="s1",
                    task=task,
                    mode=mode,
                    runtime="a",
                )
                for mode, task in (("new", "light-research"), ("resume", None))
            ],
        )
        self.assertEqual([e.kind for e in runs.parse_log(path)], ["start", "resume"])
