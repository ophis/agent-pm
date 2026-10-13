"""orchestrator router.py, ported from the old router_test (Plan, Claim, Blockers, TaskLabels, Gate
over Usage, Tick, IssueFlag, OneRouter, Outage, Usage, Brake, Outer, AttendedEntry's headless parts
as Entry) onto config.load() and a fake Driver api, plus FR-44 to FR-48, FR-56 and FR-60. A tick's
start is injected but in Entry, which runs main's own outer."""

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
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from unittest import mock

from agent_pm.linear_orchestrator.engine import config, inputs, router, runs, target
from agent_pm.linear_orchestrator.engine import run as run_py
from agent_pm.linear_orchestrator.engine.types import LinearError
from agent_pm.linear_orchestrator.linear import issues, linear
from agent_pm.session import ConfigError, DriverError, SessionStatus, Window
from agent_pm.session import Usage as SessionUsage
from agent_pm.utils import GUARD, SHORT
from orchestrator_tests.case import PYTHONPATH
from orchestrator_tests.unit import run_fixtures
from orchestrator_tests.unit.board_ids import ACCOUNTS, HEADER, TASK_GROUP, TEAM, team_node
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.board_ids import role as role_table
from orchestrator_tests.unit.clock import logs_at
from orchestrator_tests.unit.fake_api import FakeApi
from orchestrator_tests.unit.outage import FAILURES, FIELDS, failing
from orchestrator_tests.unit.run_fixtures import (
    ENGINEER,
    HARNESS,
    ID,
    KEY,
    PROJECT,
    RESEARCHER,
    SID,
    USER_NOTE,
    UUID,
    Gql,
    logged,
    node,
    show,
)
from orchestrator_tests.unit.streams import Stderr
from orchestrator_tests.unit.target_fixtures import FakeRun, ok
from orchestrator_tests.unit.target_fixtures import api as repo_info

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

CONFIG_ENG2 = (
    HEADER
    + role_table("researcher", 'next = "pm"')
    + role_table("pm", 'next = "engineer"')
    + role_table("engineer", "max_runs = 2")
    + OVERLAY
)

HUMANS = 'human_members = ["me@x.com"]\n'

FULL = ["agent-pm-researcher-TASK-5", "agent-pm-pm-TASK-6", "agent-pm-engineer-TASK-7"]

SESSIONS = [("sessions", re.escape("agent-pm-") + ".*")]

LIGHT, DEEP, STRAY, ORPHAN_LABEL = (
    f"00000000-0000-4000-8000-0000000000{n}" for n in (21, 22, 23, 24)
)

TASK_LABELS = f'[task_labels]\nlight-research = "{LIGHT}"\ndeep-research = "{DEEP}"\nproduct-design = "{ORPHAN_LABEL}"\n'

RECHECK = (
    "query($i: String!) { issue(id: $i) { state { id } assignee { id email } project { id name } "
    "labels { nodes { id name parent { id } } } } }"
)

NO = SessionStatus("no", None, "")


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def who(role):
    return None if role is None else {"id": f"u-{role}", "email": ROLE.get(role, f"{role}@x.com")}


def reading(five=10.0, weekly=10.0):
    return SessionUsage(
        None if five is None else Window(five, None),
        None if weekly is None else Window(weekly, None),
    )


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
    *,
    updated=None,
    priority=0,
    created="2026-09-01T00:00:00Z",
    project=DR,
    inverse=(),
    labels=(),
):
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


@contextlib.contextmanager
def other_router(path):
    # flock conflicts across open files in one process too
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(os.path.join(os.path.dirname(path), "router.lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


LS_REMOTE = (
    "git",
    "-c",
    "credential.helper=",
    "-c",
    "credential.helper=!gh auth git-credential",
    "ls-remote",
    "--heads",
    "https://github.com/Ophis/Agent-PM.git",
    "TASK-7-*",
)

ENG_RUN = [
    (("gh", "api", "repos/ophis/agent-pm"), repo_info("Ophis/Agent-PM")),
    (LS_REMOTE[:3], ok()),
]

LISTING = ("gh", "api", "repos/ophis/private_docs/contents/Research?ref=main")

DESIGN_LISTING = ("gh", "api", "repos/ophis/private_docs/contents/Product%20Design?ref=main")

OUTER = (
    HEADER
    + 'human_members = ["Me@X.com"]\n'
    + CONFIG.removeprefix(HEADER)
    + f'[project_repos]\n"{PROJECT}" = "ophis/agent-pm"\n'
)

INPUT = f"""Reference: TASK-7
Title: TASK-7: Session registry
Repo: Ophis/Agent-PM
Checkout: TASK-7
Branch: TASK-7-session-registry
Links: {run_fixtures.URL}

{inputs.PRECEDENCE["build"]}

## The user's requirements since the last build

Me, 2026-09-02T00:00:00.000Z:
Use SQLite.

## The user's instructions

ENG: Session registry

Add a session registry.

## PRD

None linked."""


def forwarded(assignee=ENGINEER, task=None, mode="new", sid=SID, runtime="a"):
    return [
        f"--issue={ID}",
        f"--project={PROJECT}",
        f"--assignee={assignee}",
        f"--sid={sid}",
        *([f"--task={task}"] if task else []),
        f"--mode={mode}",
        f"--runtime={runtime}",
    ]


def inner(*args, text=INPUT):
    return [
        sys.executable,
        "-P",
        "-m",
        "agent_pm.linear_orchestrator.engine.run",
        "--uuid",
        UUID,
        *args,
        f"--input={text}",
    ]


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
        if (value := self._identity(query, v)) is not None:
            return value
        if "history" in query:
            return self._issue_read(query, v)
        if "mutation" in query:
            return self._mutate(query, v)
        if "issues(filter" in query:
            return self._queue(query, v)
        if (value := self._issue_read(query, v)) is not None:
            return value
        raise AssertionError(query)

    def _identity(self, query, v):
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
        return None

    def _issue_read(self, query, v):
        if "history" in query:
            return {"issue": {"history": {"nodes": self.history.get(v["i"], [])}}}
        if query == linear.Q_ISSUE_STATE:
            return {"issue": {"state": {"id": STATES[self.issues[v["i"]]["state"]]}}}
        if "issue(id:" in query and "inverseRelations" in query:
            if v["i"] in self.unreadable:
                raise LinearError(f"linear api error: {[{'message': 'Entity not found'}]}")
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
        return None

    def _mutate(self, query, v):
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

    def _queue(self, query, v):
        f = v["f"]
        if self.todo_error and "inverseRelations" in query:
            raise LinearError(f"linear api error: {[{'message': 'Entity not found'}]}")
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
                    and (i["assignee"]["id"] in f["assignee"]["id"]["in"])
                    and (i["state"] == NAMES[f["state"]["id"]["eq"]])
                ]
            }
        }

    def reads(self, state="Todo"):
        return [
            q
            for q, v in self.queries
            if "issues(filter" in q and v["f"].get("state") == {"id": {"eq": STATES[state]}}
        ]


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
    """Each list read is followed by `after`'s edits ({ident: fields}): a change before the claim's
    recheck.
    """

    def __init__(self, issues, after):
        super().__init__(issues)
        self.after = after

    def __call__(self, query, **v):
        out = super().__call__(query, **v)
        if "issues(filter" in query:
            for ident, fields in self.after.items():
                self.issues[ident].update(fields)
        return out


class LostClaim(FakeLinear):
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


class ClaimLinear(FakeLinear):
    """FakeLinear plus the outer's issue read, answered with `node`."""

    def __init__(self, todo, node):
        super().__init__([todo])
        self.node = node

    def __call__(self, query, **v):
        if query == issues.Q_ISSUE:
            self.queries.append((query, v))
            return {"issue": self.node}
        return super().__call__(query, **v)


def two():
    return FakeLinear(
        [
            issue("TASK-1", "Todo", "researcher", priority=1),
            issue("TASK-2", "Todo", "pm", priority=2, project=PD),
        ]
    )


class Base(run_fixtures.Base):
    def setUp(self):
        super().setUp()
        self.write_local(f'work_dir = "{self.work}"\n')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.config_path = os.path.join(self.tmp, "config.toml")
        self.write_config(CONFIG)
        self.lines, self.hist, self.names = [], {}, {}
        self.logs, self.mark, self.raw, self.taken = None, 0, "", None
        self.api = FakeApi()

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
        self.add(kind="start", ident=ident, sid=sid, minutes_ago=minutes_ago, role=role)
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

    def add(self, *, kind, ident, sid, minutes_ago, role="researcher", runtime=None):
        self.lines.append(
            self.row(
                kind,
                ident,
                sid,
                role,
                runtime,
                minutes=minutes_ago,
            )
        )

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
        self.api.statuses[self.sid(sid)] = SessionStatus("yes", at, f"resume {sid}")

    def write_runs(self):
        self.write(self.runs, "".join(f"{line}\n" for line in self.lines))
        for e in runs.parse_log(self.runs):
            self.api.statuses.setdefault(e.sid, NO)

    def log_dir(self, *, again):
        if not again or self.logs is None:
            self.logs = os.path.join(self.work, "logs")
            path = os.path.join(self.logs, "orchestrator.jsonl")
            if os.path.exists(path):
                os.unlink(path)
        self.mark = len(logged(self.logs))
        return logs_at(self.logs)

    def board(self, fake, use, *, dry=False, gate=False, again=False):
        self.write_runs()
        cfg = replace(config.load(path=self.config_path), driver=self.api)
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(again=again):
            out = use(
                router.Board(
                    functools.partial(fake, service=HARNESS),
                    runs.parse_log(self.runs),
                    cfg,
                    now=NOW,
                    dry=dry,
                    gate=router.Gate(self.runs, NOW, dry=dry, driver=self.api) if gate else None,
                )
            )
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        return out

    def plan(self, fake, *, dry=False, again=False):
        def use(b):
            if cands := b.recover():
                return ("resume", *cands[0])
            return ("new",) if b.queue() else None

        run = self.board(fake, use, dry=dry, again=again)
        if run and run[0] == "resume":
            _, issue, sid = run
            return self.unmap(
                f"resume {issue['identifier']} {sid} {issue['url']} {issue['project']['name']}"
            )
        return "new" if run else ""

    def claim(self, fake, *, full=(), dry=False, recover=False, gate=False, again=False):

        def use(b):
            recover and b.recover()
            return b.take(full=full)

        self.taken = taken = self.board(fake, use, dry=dry, gate=gate, again=again)
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


class Start:
    """The injected start: records each launch Namespace; per issue an exit code (`exits`) or an
    exception to raise.
    """

    def __init__(self, exits=None, raises=None):
        self.launches, self.exits, self.raises = [], exits or {}, raises or {}

    def __call__(self, a):
        self.launches.append(a)
        if a.issue in self.raises:
            raise self.raises[a.issue]
        return self.exits.get(a.issue, 0)


class TickBase(Base):
    def tick(
        self,
        fake,
        *argv,
        hour=2,
        live=(),
        start=None,
        again=False,
        now=NOW,
        tty=False,
        outer=None,
        clock=None,
    ):
        # outer: main's run and keychain; main then starts through its own outer
        self.api.live = None if live is None else list(live)
        self.api.order, self.api.usage_calls, self.api.info = [], [], []
        self.start = None if outer else start or Start()
        self.write_runs()
        err = Stderr(tty=tty)
        with (
            redirect_stderr(err),
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
            self.log_dir(again=again),
        ):
            rc = router.main(
                list(argv),
                now=clock or (lambda: now),
                config_path=self.config_path,
                hour=hour,
                gql=fake,
                driver=self.api,
                start=self.start,
                **outer or {},
            )
            self.path = os.environ["PATH"]
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        with open(self.runs) as f:
            self.state = self.unmap(f.read())
        return rc

    def before(self):
        return self.unmap("".join(f"{line}\n" for line in self.lines))

    def last_run(self):
        entry = json.loads(self.state.splitlines()[-1])
        self.assertEqual(list(entry), ["ts", "kind", "issue", "sid", "role", "runtime"])
        return entry["kind"], entry["issue"], entry["sid"], entry["role"], entry["runtime"]

    def launched(self):
        (launch,) = self.start.launches
        return launch.issue

    def launched_runs(self):
        return [(a.issue, a.mode) for a in self.start.launches]


class OuterBase(TickBase):
    def setUp(self):
        super().setUp()
        self.write_config(OUTER)
        self.gql = Gql(issue=node(comments=[USER_NOTE]))
        self.run_ = FakeRun(ENG_RUN)
        self.missing, self.keychain_calls = set(), []

    def keychain(self, service):
        self.keychain_calls.append(service)
        return service not in self.missing

    def outer(self, *, assignee=ENGINEER, task=None, mode="new", ident=ID, sid=SID, runtime="a"):
        a = argparse.Namespace(
            issue=ident,
            project=PROJECT,
            assignee=assignee,
            sid=sid,
            task=task,
            mode=mode,
            runtime=runtime,
        )
        cfg = replace(config.load(path=self.config_path), driver=self.api)
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(again=True):
            rc = router.outer(
                a,
                cfg,
                gql=functools.partial(self.gql, service=HARNESS),
                run=self.run_,
                keychain=self.keychain,
            )
        self.err = err.getvalue()
        return rc

    def detached(self, role="engineer"):
        name, argv, cwd, env = self.api.detached[-1]
        self.assertEqual((name, cwd), (f"agent-pm-{role}-{ID}", self.rd))
        expected = {
            **os.environ,
            "PATH": config.path(),
            "PYTHONPATH": PYTHONPATH,
        }
        self.assertEqual(set(env), set(expected))
        for key, value in expected.items():
            self.assertTrue(env[key] == value, key)
        return argv

    def input(self, **kw):
        last = self.detached(**kw)[-1]
        self.assertTrue(last.startswith("--input="), last[:40])
        return last.removeprefix("--input=")


class TaskLabelsScenario(Base):
    ORPHAN = (
        'Task label "Orphan" is not one of researcher\'s tasks (deep-research, light-research). '
        "Fix the label or the assignee, then move the issue back to Todo."
    )

    def setUp(self):
        super().setUp()
        self.write_config(HUMANS + CONFIG + TASK_LABELS)


class TickScenario(TickBase):
    pass


class IssueClaims(Base):
    def test_claim_order(self):
        rows = (
            (
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        priority=0,
                        created="2026-01-01T00:00:00Z",
                    ),
                    issue(
                        "TASK-2",
                        "Todo",
                        "researcher",
                        priority=3,
                        created="2026-02-01T00:00:00Z",
                    ),
                    issue(
                        "TASK-3",
                        "Todo",
                        "researcher",
                        priority=3,
                        created="2026-01-15T00:00:00Z",
                    ),
                ],
                ["TASK-3", "TASK-2", "TASK-1"],
            ),
            (
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        priority=2,
                        created="2026-09-01T00:00:00Z",
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
                        "TASK-3",
                        "Todo",
                        "researcher",
                        priority=1,
                        created="2026-09-03T00:00:00Z",
                    ),
                ],
                ["TASK-3", "TASK-2", "TASK-1"],
            ),
            (
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        priority=2,
                        created="2026-09-01T00:00:00Z",
                    ),
                    issue(
                        "TASK-2",
                        "Todo",
                        "engineer",
                        priority=2,
                        created="2026-09-03T00:00:00Z",
                    ),
                    issue(
                        "TASK-3",
                        "Todo",
                        "pm",
                        priority=2,
                        created="2026-09-02T00:00:00Z",
                    ),
                    issue(
                        "TASK-4",
                        "Todo",
                        "engineer",
                        priority=2,
                        created="2026-09-02T00:00:00Z",
                    ),
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
            self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=300)
        self.assertEqual(
            self.claim(fake, full={"engineer"}),
            "TASK-2 https://linear.app/x/TASK-2 Deep Research",
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
            self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=300)
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
            self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=300)
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
            self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=300)
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
        with self.assertRaises(LinearError) as cm:
            self.claim(fake)
        self.assertEqual(str(cm.exception), "issueUpdate: success: false")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")


class IssueBlockers(Base):
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
            self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=300)
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
                issue(
                    "TASK-2",
                    "Todo",
                    "researcher",
                    priority=2,
                    inverse=[blocker("TASK-7")],
                ),
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
        self.assertRegex(said[0], r"^todo-error error=LinearError: linear api error: ")
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
                (
                    "inverseRelations(first: 50) { nodes { type issue { identifier state { type } } } }"
                ),
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


class RouterEntry(OuterBase):
    """router.main end to end through its own outer (the old AttendedEntry's headless parts)."""

    def setUp(self):
        super().setUp()
        self.todo = issue(ID, "Todo", "engineer")
        self.todo["project"] = {"id": PROJECT, "name": "Agent PM"}
        self.fake = ClaimLinear(self.todo, node(comments=[USER_NOTE]))

    def entry(self, *argv, live=(), tty=False, now=NOW):
        outer = {"run": self.run_, "keychain": self.keychain}
        return self.tick(self.fake, *argv, live=live, again=True, tty=tty, outer=outer, now=now)

    def test_claims_then_starts_run_py_after_its_start_line(self):
        for argv, said in (
            (("--issue", ID), []),
            (("--now",), ["launch TASK-7 exit=0"]),
        ):
            with self.subTest(argv=argv):
                self.todo["state"], self.fake.mutations, self.api.detached = "Todo", [], []
                lines, detach = [], self.api.detach

                def at_detach(*args, lines=lines, detach=detach, **kw):
                    with open(self.runs) as f:
                        lines.append(f.read())
                    return detach(*args, **kw)

                with mock.patch.object(self.api, "detach", at_detach):
                    self.assertEqual(self.entry(*argv), 0)
                kind, ident, sid, role, runtime = self.last_run()
                self.assertEqual((kind, ident, role, runtime), ("start", ID, "engineer", "a"))
                self.assertEqual(lines, [self.state])
                self.assertEqual(
                    self.detached(), inner("--target", "Ophis/Agent-PM", *forwarded(sid=sid))
                )
                self.assertEqual(
                    self.said()[-2 - len(said) :],
                    ["pick TASK-7 queue=1", "claim TASK-7 role=engineer runtime=a", *said],
                )
                self.assertEqual(
                    self.fake.mutations,
                    [(linear.M_STATE, {"i": ID, "s": IDS_BY_KEY["in_progress"]})],
                )
                self.assertEqual(self.err, "")

    def test_a_task_label_reaches_run_py_as_its_task(self):
        self.write_config(OUTER + f'[task_labels]\nlight-build = "{LIGHT}"\n')
        self.todo["labels"] = [label("Light Build", LIGHT)]
        self.assertEqual(self.entry("--issue", ID), 0)
        sid = self.last_run()[2]
        self.assertEqual(
            self.detached(),
            inner("--target", "Ophis/Agent-PM", *forwarded(task="light-build", sid=sid)),
        )
        self.assertIn("claim TASK-7 role=engineer task=light-build runtime=a", self.said())

    def test_a_resume_keeps_the_runtime_of_its_claim(self):
        self.assertEqual(self.entry(now=NOW - timedelta(minutes=60)), 0)
        sid = self.last_run()[2]
        with open(self.runs) as f:
            self.lines = f.read().splitlines()
        self.api.statuses[sid] = SessionStatus("yes", (NOW - timedelta(minutes=40)).timestamp(), "")
        self.write_config(OUTER.replace(OVERLAY, '[core]\nruntime = "b"\n'))
        self.assertEqual(self.entry(), 0)
        self.assertEqual(self.last_run(), ("resume", ID, sid, "engineer", "a"))
        self.assertEqual(
            self.detached(),
            inner("--target", "Ophis/Agent-PM", *forwarded(sid=sid, mode="resume")),
        )
        self.assertEqual((self.api.info, self.api.usage_calls), ([("a", sid, self.rd)] * 2, ["a"]))

    def test_the_outers_lines_follow_the_ticks_plan(self):
        self.todo["state"], self.fake.node = "In Progress", None
        self.resumable(ID, "s", 60, role="engineer")
        self.assertEqual(self.entry(), 0)
        self.assertEqual(
            self.said(),
            [
                "plan TASK-7 mode=resume sid=s",
                "transient TASK-7 reason=Linear: IssueError: TASK-7: issue not found",
                "launch TASK-7 exit=3",
            ],
        )

    def test_a_live_run_is_refused(self):
        for role in ("engineer", "pm"):
            with self.subTest(role=role):
                self.assertEqual(self.entry("--issue", ID, live=[f"agent-pm-{role}-{ID}"]), 1)
                self.assertEqual(
                    self.err,
                    f"router.py: {ID} has a live agent run: attach agent-pm-{role}-{ID}\n",
                )
                self.assertEqual((self.api.order, self.state), (SESSIONS, ""))
        self.assertEqual((self.fake.queries, self.api.detached), ([], []))

    def test_nothing_started_exits_1_without_a_start_line(self):
        for change, said, state in (
            (
                {"state": "In Review"},
                "pick-none TASK-7 reason=not a Todo issue assigned to a role account",
                "In Review",
            ),
            (
                {"state": "In Progress"},
                f"recover TASK-7 to=todo reason=no session updated={self.todo['updatedAt']}",
                "Todo",
            ),
            (
                {"inverseRelations": {"nodes": [blocker("TASK-9")]}},
                'blocked TASK-7 by=["TASK-9"]',
                "Todo",
            ),
            (
                {"labels": [label("Stray", STRAY)]},
                "claim-skip TASK-7 to=in_review reason=bad task label",
                "In Review",
            ),
        ):
            with self.subTest(said=said):
                self.todo.update(state="Todo", inverseRelations={"nodes": []}, labels=[])
                self.todo.update(change)
                self.assertEqual(self.entry("--issue", ID), 1)
                self.assertIn(said, self.said())
                self.assertEqual(
                    (self.todo["state"], self.state, self.api.order), (state, "", SESSIONS)
                )
        self.assertEqual(self.api.detached, [])

    def test_a_config_error_exits_1_logged_once_a_day_and_printed_on_a_terminal(self):
        for text, reason in (
            ("bogus = 1\n" + OUTER, "linear-orchestrator/config.toml: unknown keys: bogus"),
            (
                OUTER + role_table("bogus"),
                "linear-orchestrator/config.toml: role 'bogus' is not in delegate/config.toml",
            ),
        ):
            self.write_config(text)
            self.logs = None
            for argv in (["--now"], ["--issue", ID]):
                for tty, err in ((False, ""), (True, f"router.py: {reason}\n")):
                    with self.subTest(reason=reason, argv=argv, tty=tty):
                        self.assertEqual(self.entry(*argv, tty=tty), 1)
                        self.assertEqual(self.err, err)
            self.assertEqual(
                [show(e) for e in logged(self.logs)], [f"config-error reason={reason}"]
            )
        self.write_config('"bad\\nkey" = 1\n' + OUTER)
        self.logs = None
        self.assertEqual(self.entry(), 1)
        self.assertEqual(
            self.said(),
            ["config-error reason=linear-orchestrator/config.toml: unknown keys: bad key"],
        )
        self.logs = None
        self.write_local('work_dir = "w"\n')
        self.assertEqual(self.entry(), 1)
        self.assertEqual(
            (self.err, self.said()),
            (
                (
                    "linear-orchestrator/config.toml: work_dir must be a printable absolute or ~ path: 'w'\n"
                ),
                [],
            ),
        )
        self.assertEqual((self.fake.queries, self.api.order, self.api.detached), ([], [], []))


class RouterArguments(Base):
    def main(self, argv):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = router.main(
                argv,
                config_path=os.path.join(self.tmp, "absent.toml"),
                gql=None,
            )
        return rc, err.getvalue()

    def test_unknown_flags_rejected(self):
        for argv in (
            ["--help"],
            ["-h"],
            ["--prune", "x"],
            ["--gate", "new"],
            ["--now", "--issue", "TASK-1"],
            ["--issue", "TASK-1", "--now"],
            ["--brake"],
            ["--brake", "--dry-run"],
            ["--brake", "--brake"],
            ["--dry-run", "--brake", "researcher"],
            ["--brake", "researcher", "--now"],
            ["--brake", "researcher", "pm"],
            ["--brake=researcher"],
            ["--issue", "TASK-1", "--brake", "researcher"],
            ["--tui"],
            ["--now", "--tui"],
            ["--issue", "TASK-1", "--tui"],
            ["--split", "right"],
            ["--split-from", "dev"],
            ["--events", "x"],
            ["--manager", "m1"],
            ["--now=x"],
            ["--issue=TASK-1"],
            ["--now", "--issue=TASK-1"],
            ["--issue", "TASK-1", "--issue", "TASK-2"],
            ["--issue"],
            ["--now", "--issue", "--dry-run"],
            *([f, "x"] for f in ("--uuid", "--target", "--project", "--assignee", "--sid")),
            *([f, "x"] for f in ("--task", "--mode", "--runtime", "--input", "--runner")),
            ["--issue", "TASK-1", "--runtime", "a"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(argv), (2, router.USAGE + "\n"))
        self.assertEqual((self.api.order, self.api.usage_calls), ([], []))
        self.assertEqual(
            router.USAGE,
            "usage: python3 -P -m agent_pm.linear_orchestrator.engine.router [--now] [--dry-run] [--issue ID] | --brake ROLE",
        )
        for flag in ("--gate", "--tui", "--split", "--events", "--manager"):
            self.assertNotIn(flag, router.USAGE + router.__doc__)

    def test_bad_issue_id(self):
        for argv in (
            ["--issue", "task-7"],
            ["--issue", "TASK"],
            ["--dry-run", "--issue", "TASK-7x"],
        ):
            with self.subTest(argv=argv):
                ident = argv[argv.index("--issue") + 1]
                self.assertEqual(self.main(argv), (2, f"router.py: bad issue id: {ident}\n"))


class RouterLocking(TickBase):
    def test_a_tick_skips_while_another_router_runs(self):
        self.add(kind="start", ident="TASK-8", sid="old", minutes_ago=60 * 24 * 8)
        before = self.before()
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with other_router(self.runs):
            self.assertEqual(self.tick(fake), 0)
            self.assertEqual(self.said(), ["skip reason=another router is running"])
            self.assertEqual(
                (fake.queries, self.api.order, self.state, fake.issues["TASK-1"]["state"]),
                ([], [], before, "Todo"),
            )
            self.assertEqual(
                (self.tick(fake, hour=12), self.said(), self.api.order),
                (0, [], []),
            )
        self.assertEqual(self.tick(fake), 0)
        self.assertEqual(self.launched_runs(), [("TASK-1", "new")])
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_another_router_leaves_the_issue_alone(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with other_router(self.runs):
            self.assertEqual(self.tick(fake, "--issue", "TASK-1"), 1)
        self.assertEqual(self.err, "router.py: another router is running; try again\n")
        self.assertEqual(self.said(), ["skip TASK-1 reason=another router is running"])
        self.assertEqual((fake.queries, self.api.order, self.state), ([], [], ""))

    def test_a_dry_run_and_the_brake_take_no_lock(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        with other_router(self.runs):
            self.assertEqual(self.tick(fake, "--dry-run"), 0)
            self.assertEqual(self.said()[:-1], ["plan mode=new queue=1", "pick TASK-1 queue=1"])
            self.assertRegex(self.said()[-1], "^usage runtime=a ")
            self.assertEqual(self.tick(fake, "--dry-run", "--issue", "TASK-1"), 0)
            self.assertEqual(self.said(), ["pick TASK-1 queue=1"])
            out = io.StringIO()
            with redirect_stdout(out):
                rc = router.main(
                    ["--brake", "researcher"],
                    config_path=self.config_path,
                    driver=self.api,
                )
            self.assertEqual((rc, out.getvalue()), (0, "five_hour=10% weekly=10%\n"))
        self.assertEqual(fake.mutations, [])


class RouterOutages(TickBase):
    SAID = {name: f"linear-error op=teams {fields}" for name, fields in FIELDS.items()}

    def test_a_failure_at_board_setup_is_one_linear_error_a_day(self):
        for argv in (("--now",), ("--issue", "TASK-1")):
            for name, error in FAILURES.items():
                with self.subTest(argv=argv, failure=name):
                    for again in (False, True):
                        gql = failing(error)
                        self.assertEqual(self.tick(gql, *argv, again=again), 1)
                        self.assertEqual(
                            (self.said(), self.raw), ([] if again else [self.SAID[name]], "")
                        )
                        self.assertEqual(
                            gql.failed, [(linear.Q_TEAM, {"service": HARNESS, "t": TEAM})]
                        )
                        self.assertEqual((self.start.launches, self.state), ([], ""))
                    fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                    self.assertEqual(self.tick(fake, *argv), 0)
                    self.assertEqual(self.launched_runs(), [("TASK-1", "new")])

    def test_a_dry_run_prints_it_and_writes_nothing(self):
        for argv in (("--now", "--dry-run"), ("--issue", "TASK-1", "--dry-run")):
            for name, error in FAILURES.items():
                with self.subTest(argv=argv, failure=name):
                    self.assertEqual(self.tick(failing(error), *argv), 1)
                    self.assertEqual(
                        (self.said(), len(self.raw.splitlines())), ([self.SAID[name]], 1)
                    )
                    self.assertEqual((os.listdir(self.logs), self.state), (["runs.jsonl"], ""))

    def test_a_claim_applied_with_its_answer_lost_is_recovered_once_stale(self):
        for argv, plan in ((("--now",), ["plan mode=new queue=1"]), (("--issue", "TASK-1"), [])):
            with self.subTest(argv=argv):
                fake = LostClaim([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv), 1)
                self.assertEqual(
                    self.said(),
                    plan
                    + [
                        "pick TASK-1 queue=1",
                        "claim TASK-1 role=researcher runtime=a",
                        "linear-error op=issueUpdate reason=TimeoutError: timed out",
                    ],
                )
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], self.start.launches, self.state),
                    ("In Progress", [], ""),
                )
                self.assertEqual(
                    self.tick(
                        fake,
                        "--now",
                        now=NOW + router.STALE + timedelta(minutes=1),
                    ),
                    0,
                )
                self.assertEqual(
                    self.said(),
                    [
                        f"recover TASK-1 to=todo reason=no session updated={ago(minutes=0)}",
                        "plan mode=new queue=1",
                        "pick TASK-1 queue=1",
                        "claim TASK-1 role=researcher runtime=a",
                        "launch TASK-1 exit=0",
                    ],
                )
                self.assertEqual(fake.issues["TASK-1"]["comments"], [router.INTERRUPTED])


class RuntimeUsage(Base):
    """The usage gate's thresholds over the Driver's Usage (the old rate_limit_event rows)."""

    def gate(self):
        self.write_runs()
        return router.Gate(self.runs, NOW, dry=False, driver=self.api)

    def test_a_usage_probe_defect_is_not_unknown_usage(self):
        error = AttributeError("adapter defect")
        self.api.usages["a"] = error
        with self.assertRaises(AttributeError) as caught:
            router.read_usage("a", driver=self.api)
        self.assertIs(caught.exception, error)

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
            (ConfigError("no runtime a"), "reason=usage unknown"),
            (DriverError("boom"), "reason=usage unknown"),
        )
        for value, why in rows:
            with self.subTest(value=value):
                self.api.usages = {"a": value}
                with redirect_stderr(io.StringIO()), self.log_dir(again=False):
                    self.assertEqual(self.gate().blocked("a", "TASK-1"), why is not None)
                self.raw = ""
                self.assertEqual(self.said(), [f"usage-skip TASK-1 runtime=a {why}"] if why else [])

    def test_one_probe_per_runtime(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        with redirect_stderr(io.StringIO()), self.log_dir(again=False):
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


class ClaimUsageGates(Base):
    """FR-48: each candidate gates on its own runtime's usage."""

    def test_a_blocked_head_runtime_lets_the_next_runtime_claim(self):
        rows = (
            (reading(95, 10), "reason=usage usage=five_hour=95% weekly=10%"),
            (reading(10, 100), "reason=usage usage=five_hour=10% weekly=100%"),
            (None, "reason=usage unknown"),
            (ConfigError("no runtime a"), "reason=usage unknown"),
        )
        for value, why in rows:
            with self.subTest(why=why):
                self.api.usages, self.api.usage_calls = {"a": value, "b": reading(50)}, []
                fake = two()
                self.assertEqual(
                    self.claim(fake, gate=True),
                    f"TASK-2 https://linear.app/x/TASK-2 {PD}",
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
            self.claim(two(), gate=True),
            "TASK-1 https://linear.app/x/TASK-1 Deep Research",
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
        self.assertEqual(
            self.claim(fake, gate=True),
            f"TASK-2 https://linear.app/x/TASK-2 {PD}",
        )
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
            self.claim(fake, dry=True, gate=True),
            f"TASK-2 https://linear.app/x/TASK-2 {PD}",
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


class RunBrake(Base):
    """`--brake ROLE`: ROLE's runtime usage against BRAKE_5H and MAX_WEEK; no lock, no limit row."""

    def brake(self, role):
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
        ):
            rc = router.main(
                ["--brake", role],
                config_path=self.config_path,
                driver=self.api,
            )
            self.path = os.environ["PATH"]
        self.err = err.getvalue()
        return rc, out.getvalue()

    def test_blocks_at_brake_5h(self):
        self.assertEqual(router.BRAKE_5H, 80)
        rows = (
            (reading(70), 0, "five_hour=70% weekly=10%"),
            (reading(79.9, 99.9), 0, "five_hour=79.9% weekly=99.9%"),
            (reading(80), 1, "five_hour=80% weekly=10%"),
            (reading(10, 100), 1, "five_hour=10% weekly=100%"),
            (reading(None, None), 0, "five_hour=none weekly=none"),
            (None, 1, "unknown"),
            (ConfigError("no runtime a"), 1, "unknown"),
            (DriverError("boom"), 1, "unknown"),
        )
        for value, rc, out in rows:
            with self.subTest(value=value):
                self.api.usages, self.api.usage_calls = {"a": value}, []
                self.assertEqual(self.brake("researcher"), (rc, out + "\n"))
                self.assertEqual((self.api.usage_calls, self.err), (["a"], ""))
        self.assertEqual(self.path, config.path())

    def test_the_roles_own_runtime(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        self.assertEqual(self.brake("pm"), (0, "five_hour=50% weekly=10%\n"))
        self.assertEqual(self.api.usage_calls, ["b"])

    def test_no_such_role(self):
        self.assertEqual(self.brake("ghost"), (2, ""))
        self.assertEqual((self.err, self.api.usage_calls), ("router.py: no role ghost\n", []))

    def test_a_live_limit_row_is_not_read(self):
        self.limit("a", (datetime.now(UTC) + timedelta(hours=1)).isoformat())
        self.write_runs()
        self.api.usages = {"a": reading(70)}
        self.assertEqual(self.brake("researcher"), (0, "five_hour=70% weekly=10%\n"))
        self.assertEqual(self.api.usage_calls, ["a"])


class ScheduledUsageGates(TickBase):
    """FR-46, FR-48, FR-60 over a tick: each candidate gates on its runtime; a resume keeps its
    recorded runtime.
    """

    def test_a_resume_keeps_the_runtime_of_its_claim(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, now=NOW - timedelta(minutes=60))
        (claimed,) = self.start.launches
        self.assertEqual(self.last_run(), ("start", "TASK-1", claimed.sid, "researcher", "a"))
        with open(self.runs) as f:
            self.lines = f.read().splitlines()
        self.api.statuses[claimed.sid] = SessionStatus(
            "yes", (NOW - timedelta(minutes=40)).timestamp(), ""
        )
        self.write_config(CONFIG.replace(OVERLAY, '[core]\nruntime = "b"\n'))
        self.tick(fake)
        (resumed,) = self.start.launches
        self.assertEqual((resumed.sid, resumed.mode, resumed.runtime), (claimed.sid, "resume", "a"))
        self.assertEqual(self.last_run(), ("resume", "TASK-1", claimed.sid, "researcher", "a"))
        self.assertEqual(
            (self.api.info, self.api.usage_calls),
            ([("a", claimed.sid, os.path.join(self.work, "work", "TASK-1"))], ["a"]),
        )

    def test_a_blocked_resume_candidate_lets_a_claim_start(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=1),
            issue("TASK-2", "Todo", "pm", priority=2, project=PD),
        )
        self.resumable("TASK-1", "a", 60)
        self.assertEqual(self.tick(fake), 0)
        self.assertEqual(
            self.said(),
            [
                "usage-skip TASK-1 runtime=a reason=usage usage=five_hour=95% weekly=10%",
                "plan mode=new queue=1",
                "pick TASK-2 queue=1",
                "claim TASK-2 role=pm runtime=b",
                "launch TASK-2 exit=0",
            ],
        )
        (launch,) = self.start.launches
        self.assertEqual((launch.issue, launch.runtime), ("TASK-2", "b"))
        self.assertEqual(self.last_run(), ("start", "TASK-2", launch.sid, "pm", "b"))
        self.assertEqual(self.api.usage_calls, ["a", "b"])
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_all_blocked_starts_nothing_and_probes_a_runtime_once(self):
        self.api.usages = {"a": reading(95)}
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=1),
            issue("TASK-3", "In Progress", "engineer", priority=2, project=PD),
            issue("TASK-2", "Todo", "researcher", priority=2),
        )
        self.resumable("TASK-1", "a", 60)
        self.resumable("TASK-3", "c", 50, "engineer")
        before = self.before()
        why = "runtime=a reason=usage usage=five_hour=95% weekly=10%"
        for again, said in ((False, [f"usage-skip TASK-{n} {why}" for n in (1, 3, 2)]), (True, [])):
            with self.subTest(again=again):
                self.assertEqual(self.tick(fake, again=again), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual(
                    (self.start.launches, fake.mutations, self.state, self.api.usage_calls),
                    ([], [], before, ["a"]),
                )

    def test_a_live_limit_row_lets_the_other_runtime_start(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        self.limit("a", at)
        self.tick(two())
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=2",
                "pick TASK-1 queue=2",
                f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                "pick TASK-2 queue=2",
                "claim TASK-2 role=pm runtime=b",
                "launch TASK-2 exit=0",
            ],
        )
        self.assertEqual((self.launched_runs(), self.api.usage_calls), ([("TASK-2", "new")], ["b"]))
        self.assertEqual(
            [json.loads(x)["kind"] for x in self.state.splitlines()], ["limit", "start"]
        )

    def test_a_dry_run_walks_the_same_and_changes_nothing(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        b = "usage runtime=b usage=five_hour=10% weekly=10% allowed=true"
        rows = (
            (
                True,
                [
                    f"usage runtime=a until={at} allowed=false",
                    f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                    "plan mode=new queue=1",
                    "pick TASK-2 queue=1",
                    b,
                ],
                ["b"],
            ),
            (
                False,
                [
                    "usage runtime=a usage=five_hour=10% weekly=10% allowed=true",
                    "plan TASK-1 mode=resume sid=a",
                ],
                ["a"],
            ),
        )
        for limited, said, calls in rows:
            with self.subTest(limited=limited):
                self.lines, self.hist = [], {}
                if limited:
                    self.limit("a", at)
                fake = self.fake(
                    issue("TASK-1", "In Progress", "researcher", priority=1),
                    issue("TASK-2", "Todo", "pm", priority=2, project=PD),
                )
                self.resumable("TASK-1", "a", 60)
                before = self.before()
                self.assertEqual(self.tick(fake, "--dry-run"), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual(
                    (self.start.launches, fake.mutations, self.state, self.api.usage_calls),
                    ([], [], before, calls),
                )


class TaskSelection(TaskLabelsScenario):
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
        absent = "is not in ~/.agent-pm/config.local.toml's [task_labels]. Fix the label, then move the issue back to Todo."
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
                    (
                        f'Task label "Orphan" is not one of researcher\'s tasks (deep-research, light-research). {fix}'
                    ),
                ),
            ),
            (
                [label("Light Research", LIGHT), outside[0], label("Deep Research", DEEP)],
                (
                    None,
                    (
                        "Several task labels (Light Research, Deep Research); keep one, then move the issue back to Todo."
                    ),
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
            self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=300)
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
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        labels=[label("Orphan", ORPHAN_LABEL)],
                    )
                ]
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
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    labels=[label("Light Research", LIGHT)],
                )
            ]
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


class ScheduledTaskSelection(TickBase):
    def setUp(self):
        super().setUp()
        self.write_config(HUMANS + CONFIG + TASK_LABELS)

    def test_fr1_tick_checks_the_task_group_once_and_stops_on_a_bad_one(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake)
        self.assertEqual(
            [v for q, v in fake.queries if q == linear.Q_TASK_GROUP], [{"i": TASK_GROUP}]
        )
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        fake.group = {"isGroup": False}
        self.assertEqual(self.tick(fake), 1)
        self.assertIn(
            f"config-error reason=linear-orchestrator/config.toml: task_label_group {TASK_GROUP} is not a label group",
            self.said(),
        )
        self.assertEqual(
            (fake.queries, fake.mutations),
            ([(linear.Q_TEAM, {"t": TEAM}), (linear.Q_TASK_GROUP, {"i": TASK_GROUP})], []),
        )
        self.assertEqual(
            (self.api.order, self.api.usage_calls, self.start.launches), (SESSIONS, [], [])
        )

    def test_blocked_invalid_label_waits_for_its_blocker(self):
        for argv, plan, end, rc in (
            ((), ["plan mode=new queue=1"], ["skip reason=nothing claimed"], 0),
            (("--issue", "TASK-1"), [], [], 1),
        ):
            with self.subTest(argv=argv):
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
                self.assertEqual(self.tick(fake, *argv), rc)
                self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7"]'])
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], fake.mutations, self.start.launches),
                    ("Todo", [], []),
                )
                self.assertNotIn(RECHECK, [q for q, _ in fake.queries])
                fake.issues["TASK-1"]["inverseRelations"]["nodes"][0]["issue"]["state"]["type"] = (
                    "completed"
                )
                self.assertEqual(self.tick(fake, *argv), rc)
                t = fake.issues["TASK-1"]
                self.assertEqual(
                    (t["state"], t["comments"]), ("In Review", [TaskLabelsScenario.ORPHAN])
                )
                self.assertEqual(
                    self.said(),
                    plan
                    + [
                        "pick TASK-1 queue=1",
                        "claim-skip TASK-1 to=in_review reason=bad task label",
                        "pick-none reason=nothing claimable",
                    ]
                    + end,
                )
                self.assertEqual((self.start.launches, self.state), ([], ""))

    def test_fr20_resume_passes_no_task(self):
        fake = self.fake(
            issue(
                "TASK-1",
                "In Progress",
                "researcher",
                labels=[label("Light Research", LIGHT)],
            )
        )
        self.moved("TASK-1", 61)
        self.add(kind="resume", ident="TASK-1", sid="a", minutes_ago=60)
        self.touch("a", 40)
        self.tick(fake)
        (launch,) = self.start.launches
        self.assertEqual((launch.sid, launch.task, launch.mode), (self.sid("a"), None, "resume"))
        self.assertEqual(self.last_run(), ("resume", "TASK-1", "a", "researcher", "a"))
        self.assertIn("plan TASK-1 mode=resume sid=a", self.said())

    def test_fr22_a_logged_role_not_the_assignees_goes_to_review(self):
        fake = self.fake(issue("TASK-1", "In Progress", "engineer", project=PD))
        self.resumable("TASK-1", "a", 60)
        before = self.before()
        self.tick(fake)
        t = fake.issues["TASK-1"]
        self.assertEqual(
            (t["state"], t["assignee"], t["comments"], t["subscribers"]),
            (
                "In Review",
                who("engineer"),
                [
                    "The interrupted agent run's role (researcher) is not the "
                    "assignee's (engineer); needs a look."
                ],
                ["me@x.com"],
            ),
        )
        self.assertEqual(
            writes(fake),
            [
                ("issueSubscribe", "TASK-1"),
                ("read", "TASK-1"),
                ("issueUpdate", "TASK-1"),
                ("commentCreate", "TASK-1"),
            ],
        )
        self.assertEqual(fake.mutations[1][1]["s"], STATES["In Review"])
        self.assertEqual(
            self.said(),
            [
                (
                    "recover TASK-1 to=in_review reason=role (researcher) is not the assignee's (engineer)"
                )
            ],
        )
        self.assertEqual((self.start.launches, self.state), ([], before))

    def test_fr16_requeued_issue_resolved_again_at_claim(self):
        fake = self.fake(
            issue(
                "TASK-1",
                "In Progress",
                "researcher",
                labels=[label("Light Research", LIGHT)],
            )
        )
        self.moved("TASK-1", 61)
        self.add(kind="start", ident="TASK-1", sid="a", minutes_ago=60)
        self.tick(fake)
        self.assertEqual(fake.issues["TASK-1"]["comments"], [router.INTERRUPTED])
        self.assertIn("claim TASK-1 role=researcher task=light-research runtime=a", self.said())
        (launch,) = self.start.launches
        self.assertEqual((launch.task, launch.mode), ("light-research", "new"))
        self.assertEqual(self.last_run(), ("start", "TASK-1", launch.sid, "researcher", "a"))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")


class RunLaunching(OuterBase):
    """FR-45, FR-47's resume check: the outer reads the issue, checks its repo, renders its input
    and starts run.py in a detached Driver session.
    """

    def local_clone(self):
        path = os.path.realpath(os.path.join(self.tmp, "clone"))
        for argv in (
            ["init", "-q", path],
            ["-C", path, "remote", "add", "origin", "https://github.com/ophis/agent-pm.git"],
        ):
            subprocess.run(["git", *GUARD, *argv], check=True)
        self.write_config(OUTER + f'[local_clones]\n"Ophis/Agent-PM" = "{path}"\n')
        return path

    def test_build_starts_run_py_in_a_detached_driver_session(self):
        self.assertEqual(self.outer(), 0)
        self.assertEqual(self.detached(), inner("--target", "Ophis/Agent-PM", *forwarded()))
        self.assertEqual((len(self.api.detached), os.listdir(self.rd)), (1, []))
        self.assertEqual(self.gql.calls, [("issue", HARNESS, {"i": ID})])
        self.assertEqual(
            self.run_.calls, [(("gh", "api", "repos/ophis/agent-pm"), SHORT), (LS_REMOTE, SHORT)]
        )
        self.assertEqual(
            (os.environ["PATH"], self.keychain_calls, self.api.info), (config.path(), [KEY], [])
        )
        self.assertEqual((self.err, self.said()), ("", []))

    def test_a_given_task_reaches_run_py_whose_argv_parses_it(self):
        self.assertEqual(self.outer(task="light-build"), 0)
        argv = self.detached()
        self.assertEqual(argv, inner("--target", "Ophis/Agent-PM", *forwarded(task="light-build")))
        found = run_py.options(argv[4:])
        self.assertEqual(
            (
                found.uuid,
                found.target,
                found.issue,
                found.project,
                found.assignee,
                found.sid,
                found.task,
                found.mode,
                found.runtime,
            ),
            (UUID, "Ophis/Agent-PM", ID, PROJECT, ENGINEER, SID, "light-build", "new", "a"),
        )
        self.assertEqual((found.input, found.tui), (INPUT, False))

    def test_a_long_input_reaches_run_py_intact(self):
        big = "".join(
            f"Requirement {i}: keep the session registry small.\n" for i in range(500)
        ).strip()
        self.gql.issue = node(description=big, comments=[USER_NOTE])
        self.assertEqual(self.outer(), 0)
        text = INPUT.replace("Add a session registry.", big)
        self.assertGreater(len(text.encode()), 20_000)
        self.assertEqual(
            self.detached(), inner("--target", "Ophis/Agent-PM", *forwarded(), text=text)
        )

    def test_nul_is_removed_and_a_lone_surrogate_replaced_in_the_input(self):
        for description, want in (
            ("Add a session\0 registry.\0", INPUT),
            (
                "Add a session\ud800 registry.",
                INPUT.replace("Add a session registry.", "Add a session? registry."),
            ),
        ):
            with self.subTest(description=description):
                self.gql.issue = node(description=description, comments=[USER_NOTE])
                self.assertEqual(self.outer(), 0)
                self.assertEqual(self.input(), want)

    def test_research_and_design_have_no_target(self):
        rows = (
            (RESEARCHER, "researcher", LISTING, "research", "Question", "Compare queues", "Which?"),
            (ROLE["pm"], "pm", DESIGN_LISTING, "design", "Brief", "Queues PRD", "Build a PRD."),
        )
        for who_, role, listing, kind, heading, title, description in rows:
            with self.subTest(role=role):
                self.gql.issue = node(title=title, description=description)
                self.run_.table, self.run_.calls = [(listing, ok("[]"))], []
                self.assertEqual(self.outer(assignee=who_), 0)
                self.assertEqual(
                    self.detached(role),
                    inner(
                        *forwarded(who_),
                        text=(
                            f"Reference: TASK-7\nRepo: ophis/agent-pm\nCheckout: TASK-7\n\n"
                            f"{inputs.PRECEDENCE[kind]}\n\n## {heading}\n\n{title}\n\n{description}"
                        ),
                    ),
                )
                self.assertEqual(self.run_.calls, [(listing, SHORT)])

    def test_a_configured_local_clone_is_the_inputs_repo(self):
        clone = self.local_clone()
        self.assertEqual(self.outer(), 0)
        self.assertEqual(self.input(), INPUT.replace("Repo: Ophis/Agent-PM", f"Repo: {clone}"))
        self.assertEqual(
            self.run_.calls, [(("gh", "api", "repos/ophis/agent-pm"), SHORT), (LS_REMOTE, SHORT)]
        )
        self.assertEqual(self.err, "")

    def test_research_and_design_name_the_local_clone(self):
        clone = self.local_clone()
        for who_, role, listing in (
            (RESEARCHER, "researcher", LISTING),
            (ROLE["pm"], "pm", DESIGN_LISTING),
        ):
            with self.subTest(who=who_):
                self.run_.table = [(listing, ok("[]"))]
                self.assertEqual(self.outer(assignee=who_), 0)
                argv = self.detached(role)
                self.assertNotIn("--target", argv)
                self.assertEqual(argv[-1].splitlines()[1], f"Repo: {clone}")

    def test_a_resume_over_a_clone_checkout_keeps_its_repo(self):
        self.local_clone()
        cfg = replace(config.load(path=self.config_path), driver=self.api)
        os.makedirs(
            os.path.join(target.checkout(ID, "Ophis", "Agent-PM", work=cfg.runs_dir), ".git")
        )
        self.run_.table = [(("git", *GUARD, "-C"), ok()), *ENG_RUN]
        self.assertEqual(self.outer(mode="resume"), 0)
        self.assertEqual(self.input(), INPUT)
        self.assertEqual([argv[0] for argv, _ in self.run_.calls], ["gh", "git", "git"])

    def test_a_resume_reads_its_session_at_its_runtime_and_rebuilds_the_input(self):
        self.assertEqual(self.outer(mode="resume", runtime="b"), 0)
        self.assertEqual(
            self.detached(),
            inner("--target", "Ophis/Agent-PM", *forwarded(mode="resume", runtime="b")),
        )
        self.assertEqual(self.api.info, [("b", SID, self.rd)])

    def test_a_status_defect_does_not_become_a_transient_resume(self):
        error = AttributeError("adapter defect")
        self.api.statuses[SID] = error
        with self.assertRaises(AttributeError) as caught:
            self.outer(mode="resume")
        self.assertIs(caught.exception, error)
        self.assertEqual((self.api.detached, self.gql.calls), ([], []))

    def test_a_resume_not_resumable_starts_nothing(self):
        for value, why in (
            (NO, "no"),
            (SessionStatus("unknown", None, ""), "unknown"),
            (DriverError("boom"), "DriverError: boom"),
        ):
            with self.subTest(why=why):
                self.api.statuses[SID] = value
                self.assertEqual(self.outer(mode="resume"), 3)
                self.assertEqual(
                    (self.said(), self.err),
                    ([f"transient TASK-7 reason=session {SID} not resumable ({why})"], ""),
                )
        self.assertEqual((self.api.detached, self.gql.calls, self.run_.calls), ([], [], []))
        self.assertEqual(self.api.info, [("a", SID, self.rd)] * 3)

    def test_bad_issue_or_session_id(self):
        for ident, sid in (("task-7", SID), (ID, "not-a-sid"), (ID, "z" * 36)):
            with self.subTest(ident=ident, sid=sid):
                self.assertEqual(self.outer(ident=ident, sid=sid), 2)
                self.assertEqual(self.err, f"router.py: bad issue or session id: {ident} {sid}\n")
        self.assertEqual((self.api.detached, self.gql.calls, self.keychain_calls), ([], [], []))

    def test_role_and_task_errors_are_config_errors_of_the_issue(self):
        for kw, reason in (
            ({"assignee": "x@y.com"}, "'x@y.com' is not a role account"),
            (
                {"assignee": RESEARCHER, "task": "build"},
                "task 'build' is not one of researcher's tasks (deep-research, light-research)",
            ),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(self.outer(mode="resume", **kw), 2)
                self.assertEqual(
                    (self.said(), self.err), ([f"config-error TASK-7 reason={reason}"], "")
                )
        self.assertEqual(
            (self.api.detached, self.api.info, self.gql.calls, self.keychain_calls),
            ([], [], [], []),
        )

    def test_no_keychain_item(self):
        self.missing.add(KEY)
        for mode in ("new", "resume"):
            with self.subTest(mode=mode):
                self.assertEqual(self.outer(mode=mode), 2)
                self.assertEqual(
                    self.said(),
                    [f"config-error TASK-7 reason=no Keychain item for role key {KEY}"],
                )
        self.assertEqual((self.api.detached, self.api.info, self.gql.calls), ([], [], []))

    def test_linear_errors_are_transient(self):
        for gql, reason in (
            (Gql(), "IssueError: TASK-7: issue not found"),
            (
                Gql(lambda _n, _v: LinearError("linear api error: boom"), node()),
                "LinearError: linear api error: boom",
            ),
        ):
            with self.subTest(reason=reason):
                self.gql = gql
                self.assertEqual(self.outer(), 3)
                self.assertEqual(self.said(), [f"transient TASK-7 reason=Linear: {reason}"])
        self.assertEqual((self.api.detached, self.run_.calls), ([], []))

    def test_invalid_new_bounces_without_a_run(self):
        self.run_.table = [(("gh", "api"), ok(code=1, stderr="gh: Not Found (HTTP 404)"))]
        reason = "project mapping ophis/agent-pm: not found or no access (HTTP 404)"
        for task in (None, "light-build"):
            with self.subTest(task=task):
                self.gql = Gql(issue=node(comments=[USER_NOTE]))
                self.assertEqual(self.outer(task=task), 0)
                self.assertEqual(
                    self.gql.calls,
                    [
                        ("issue", HARNESS, {"i": ID}),
                        ("subscribe", KEY, {"i": UUID, "e": "me@x.com"}),
                        (
                            "post",
                            KEY,
                            {
                                "i": UUID,
                                "b": (
                                    f"Question: repo check failed: {reason}. Fix the description's "
                                    "`Repo:` line, then move this issue back to Todo."
                                ),
                            },
                        ),
                        ("reread", KEY, {"i": UUID}),
                        ("state", KEY, {"i": UUID, "s": IDS_BY_KEY["in_review"]}),
                    ],
                )
                self.assertEqual(self.said(), [f"bounce TASK-7 reason={reason}"])
                self.assertEqual((self.api.detached, os.path.exists(self.rd)), ([], False))

    def test_bounce_failure_is_transient(self):
        self.run_.table = [(("gh", "api"), ok(code=1, stderr="HTTP 404"))]
        self.gql = Gql(
            lambda n, _v: LinearError("linear api error: boom") if n == "post" else None, node()
        )
        self.assertEqual(self.outer(), 3)
        self.assertEqual(
            self.said(), ["transient TASK-7 reason=bounce: LinearError: linear api error: boom"]
        )
        self.assertEqual(self.api.detached, [])

    def test_invalid_resume_and_transient_check_start_nothing(self):
        for mode, r, reason in (
            (
                "resume",
                ok(code=1, stderr="HTTP 404"),
                "project mapping ophis/agent-pm: not found or no access (HTTP 404)",
            ),
            (
                "new",
                ok(code=1, stderr="HTTP 502: Bad Gateway"),
                "gh api repos/ophis/agent-pm: HTTP 502: Bad Gateway",
            ),
            (
                "new",
                subprocess.TimeoutExpired(["gh", "api"], 60),
                "repo check: TimeoutExpired: Command '['gh', 'api']' timed out after 60 seconds",
            ),
        ):
            with self.subTest(reason=reason):
                self.run_.table = [(("gh", "api"), r)]
                self.assertEqual(self.outer(mode=mode), 3)
                self.assertEqual(self.said(), [f"transient TASK-7 reason={reason}"])
        self.assertEqual(self.gql.calls, [("issue", HARNESS, {"i": ID})] * 3)
        self.assertEqual(self.api.detached, [])

    def test_docs_failure_is_transient(self):
        self.gql.issue = node(title="Compare queues", description="Which queue fits?")
        for r, reason in (
            (ok(code=1, stderr="HTTP 500"), "InputError: gh api contents Research: HTTP 500"),
            (OSError("gh missing"), "OSError: gh missing"),
        ):
            with self.subTest(reason=reason):
                self.run_.table = [(LISTING, r)]
                self.assertEqual(self.outer(assignee=RESEARCHER), 3)
                self.assertEqual(self.said(), [f"transient TASK-7 reason=docs: {reason}"])
        self.assertEqual(self.api.detached, [])

    def test_a_run_dir_that_cannot_be_made_starts_nothing(self):
        self.write(self.rd, "")
        self.assertEqual(self.outer(), 1)
        self.assertEqual(
            (self.said(), self.err, self.api.detached),
            (
                [
                    (
                        f"launch-error TASK-7 error=input: FileExistsError: [Errno 17] File exists: '{self.rd}'"
                    )
                ],
                "",
                [],
            ),
        )

    def test_a_driver_session_that_cannot_start_exits_1(self):
        for error in (
            DriverError("detach agent-pm-engineer-TASK-7: duplicate session"),
            ConfigError("cwd must be an existing directory"),
        ):
            with self.subTest(error=error):
                self.api.detach_error = error
                self.assertEqual(self.outer(), 1)
                self.assertEqual(
                    (self.said(), self.err),
                    ([f"launch-error TASK-7 error={type(error).__name__}: {error}"], ""),
                )
        self.assertEqual(self.input(), INPUT)


class RunStartRecords(Base):
    """FR-46: the start or resume line records the runtime; the launch Namespace carries it."""

    def test_one_json_line_and_the_launch_namespace(self):
        path = os.path.join(self.tmp, "logs", "runs.jsonl")
        found = issue("TASK-1", "In Progress", "researcher")
        stamp = datetime.fromisoformat("2026-10-09T00:26:33-04:00").astimezone().isoformat()
        got = [
            router.begin(
                path,
                mode=mode,
                issue=found,
                sid="s1",
                role="researcher",
                task=task,
                runtime="a",
                now=lambda: datetime.fromisoformat(stamp),
            )
            for mode, task in (("new", "light-research"), ("resume", None))
        ]
        with open(path) as f:
            self.assertEqual(
                f.read(),
                "".join(
                    f'{{"ts": "{stamp}", "kind": "{kind}", "issue": "TASK-1", '
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


class ExplicitIssueRuns(TickBase):
    def nothing_resumable(self):
        def capped():
            for m in (390, 380, 370, 360):
                self.add(kind="start", ident="TASK-1", sid=f"s{m}", minutes_ago=m)

        return [
            (
                "cap",
                issue("TASK-1", "In Progress", "researcher"),
                capped,
                "recover TASK-1 to=in_review reason=reached 4 attempts",
                "In Review",
                router.CAP_COMMENT,
            ),
            (
                "role",
                issue("TASK-1", "In Progress", "researcher"),
                lambda: self.resumable("TASK-1", "a", 60, "engineer"),
                (
                    "recover TASK-1 to=in_review reason=role (engineer) is not the assignee's (researcher)"
                ),
                "In Review",
                (
                    "The interrupted agent run's role (engineer) is not the assignee's (researcher); needs a look."
                ),
            ),
            (
                "not resumable",
                issue("TASK-1", "In Progress", "researcher"),
                lambda: self.add(kind="start", ident="TASK-1", sid="n", minutes_ago=5),
                "recover TASK-1 to=todo reason=not resumable sid=n",
                "Todo",
                router.INTERRUPTED,
            ),
            (
                "no sid",
                issue("TASK-1", "In Progress", "researcher", updated=ago(minutes=5)),
                lambda: None,
                f"recover TASK-1 to=todo reason=no session updated={ago(minutes=5)}",
                "Todo",
                router.INTERRUPTED,
            ),
        ]

    def test_todo_is_claimed_and_started_whatever_the_gates(self):
        self.add(kind="start", ident="TASK-8", sid="old", minutes_ago=60 * 24 * 8)
        self.limit("a", (NOW + timedelta(hours=1)).isoformat())
        self.api.usages = {"a": reading(95)}
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3)),
            issue("TASK-2", "In Progress", "researcher", priority=1),
            issue("TASK-3", "Todo", "researcher", priority=1),
            issue("TASK-4", "Todo", "engineer", priority=3, project=PD),
        )
        self.resumable("TASK-2", "b", 60)
        self.assertEqual(self.tick(fake, "--issue", "TASK-4", hour=12, live=FULL), 0)
        self.assertEqual(
            self.said(), ["pick TASK-4 queue=1", "claim TASK-4 role=engineer runtime=a"]
        )
        (launch,) = self.start.launches
        self.assertEqual(
            launch,
            argparse.Namespace(
                issue="TASK-4",
                project=IDS[PD],
                assignee=ROLE["engineer"],
                sid=launch.sid,
                task=None,
                mode="new",
                runtime="a",
            ),
        )
        self.assertRegex(launch.sid, config.UUID_RE)
        self.assertEqual(self.last_run(), ("start", "TASK-4", launch.sid, "engineer", "a"))
        self.assertIn("TASK-8", self.state)
        self.assertEqual((self.api.order, self.api.usage_calls), (SESSIONS, []))
        self.assertEqual(
            {i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
            {
                "TASK-1": ("In Progress", None),
                "TASK-2": ("In Progress", None),
                "TASK-3": ("Todo", None),
                "TASK-4": ("In Progress", None),
            },
        )
        self.assertEqual(len(fake.reads()), 1)

    def test_exits_with_the_outers_code(self):
        for rc in (0, 1, 3):
            with self.subTest(rc=rc):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                start = Start(exits={"TASK-1": rc})
                self.assertEqual(self.tick(fake, "--issue", "TASK-1", start=start), rc)

    def test_unknown_agent_runs_start_nothing(self):
        for dry in (False, True):
            with self.subTest(dry=dry):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                argv = ("--issue", "TASK-1", *["--dry-run"] * dry)
                self.assertEqual(self.tick(fake, *argv, live=None), 0 if dry else 1)
                self.assertEqual(self.err, "router.py: agent runs unknown; try again\n")
                self.assertEqual(
                    (fake.queries, self.start.launches, self.state, self.said()), ([], [], "", [])
                )

    def test_a_live_agent_run_is_refused_with_its_attach_command(self):
        for state in ("Todo", "In Progress", "In Review"):
            for dry in (False, True):
                with self.subTest(state=state, dry=dry):
                    fake = FakeLinear([issue("TASK-1", state, "engineer")])
                    argv = ("--issue", "TASK-1", *["--dry-run"] * dry)
                    self.assertEqual(
                        self.tick(fake, *argv, live=["agent-pm-engineer-TASK-1"]),
                        0 if dry else 1,
                    )
                    self.assertEqual(
                        self.err,
                        "router.py: TASK-1 has a live agent run: attach agent-pm-engineer-TASK-1\n",
                    )
                    self.assertEqual(
                        (fake.queries, self.api.order, self.state, self.start.launches),
                        ([], SESSIONS, "", []),
                    )

    def test_in_progress_resumes_inside_recovers_time_windows(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.resumable("TASK-1", "a", 20)
        self.touch("a", 5)
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(self.tick(fake, "--issue", "TASK-1", hour=12), 0)
        self.assertEqual(self.said(), ["plan TASK-1 mode=resume sid=a"])
        (launch,) = self.start.launches
        self.assertEqual(
            launch,
            argparse.Namespace(
                issue="TASK-1",
                project=IDS[DR],
                assignee=ROLE["researcher"],
                sid=self.sid("a"),
                task=None,
                mode="resume",
                runtime="a",
            ),
        )
        self.assertEqual(self.last_run(), ("resume", "TASK-1", "a", "researcher", "a"))
        self.assertEqual((fake.mutations, self.api.usage_calls), ([], []))

    def test_in_progress_with_nothing_to_resume_is_settled_at_once(self):
        self.write_config(HUMANS + CONFIG)
        for name, found, given, said, state, comment in self.nothing_resumable():
            with self.subTest(name=name):
                self.lines, self.hist = [], {}
                given()
                fake = self.fake(found)
                before = self.before()
                if name in ("not resumable", "no sid"):
                    self.assertEqual((self.plan(fake), fake.mutations), ("", []))
                self.assertEqual(self.tick(fake, "--issue", "TASK-1"), 1)
                self.assertEqual(self.said(), [said])
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["comments"]),
                    (state, [comment]),
                )
                self.assertEqual(
                    (self.start.launches, self.api.usage_calls, self.state), ([], [], before)
                )

    def test_dry_run_prints_the_decision_only(self):
        def todo():
            return self.fake(issue("TASK-1", "Todo", "researcher"))

        def resumable():
            self.resumable("TASK-1", "a", 20)
            return self.fake(issue("TASK-1", "In Progress", "researcher"))

        cases = [
            ("todo", todo, ["pick TASK-1 queue=1"]),
            ("resume", resumable, ["plan TASK-1 mode=resume sid=a"]),
        ]
        cases += [
            (name, lambda found=found, given=given: (given(), self.fake(found))[1], [said])
            for name, found, given, said, _, _ in self.nothing_resumable()
        ]
        for name, given, said in cases:
            with self.subTest(name=name):
                self.lines, self.hist = [], {}
                fake = given()
                before = self.before()
                self.assertEqual(self.tick(fake, "--dry-run", "--issue", "TASK-1"), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual(
                    (fake.issues["TASK-1"]["state"], fake.mutations),
                    ("Todo" if name == "todo" else "In Progress", []),
                )
                self.assertEqual(
                    (self.start.launches, self.api.usage_calls, self.state), ([], [], before)
                )
                self.assertNotIn(RECHECK, [q for q, _ in fake.queries])

    def test_todo_not_claimable_starts_nothing(self):
        self.write_config(HUMANS + CONFIG)
        for argv in (("--issue", "TASK-1"), ("--issue", "TASK-1", "--dry-run")):
            with self.subTest(argv=argv):
                self.lines = []
                for sid in "abcd":
                    self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=300)
                before = self.before()
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv), 0 if "--dry-run" in argv else 1)
                self.assertEqual(
                    self.said(),
                    [
                        "claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                        "pick-none reason=nothing claimable",
                    ],
                )
                self.assertEqual(
                    fake.issues["TASK-1"]["state"], "Todo" if "--dry-run" in argv else "In Review"
                )
                self.assertEqual((self.start.launches, self.state), ([], before))

    def test_blocked_starts_nothing(self):
        for dry in (False, True):
            for resume in (False, True):
                with self.subTest(dry=dry, resume=resume):
                    self.lines, self.hist = [], {}
                    listed = [
                        issue(
                            "TASK-1",
                            "Todo",
                            "researcher",
                            inverse=[blocker("TASK-7")],
                        ),
                        issue("TASK-4", "Todo", "pm", project=PD),
                    ]
                    if resume:
                        listed.append(issue("TASK-2", "In Progress", "researcher"))
                        self.resumable("TASK-2", "a", 60)
                    fake = FakeLinear(listed, self.hist)
                    before = self.before()
                    self.assertEqual(
                        self.tick(fake, "--issue", "TASK-1", *["--dry-run"] * dry), 0 if dry else 1
                    )
                    self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7"]'])
                    self.assertEqual(
                        (self.api.usage_calls, self.start.launches, fake.mutations, self.state),
                        ([], [], [], before),
                    )
                    self.assertEqual(len(fake.reads()), 1)

    def test_another_state_or_not_a_role_account_starts_nothing(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "In Review", "researcher"),
                issue("TASK-2", "Todo", "someone"),
                issue("TASK-3", "Todo"),
                issue("TASK-4", "In Progress", "someone", updated=ago(hours=3)),
                issue("TASK-5", "Todo", "researcher", project=None),
            ]
        )
        for ident in ("TASK-1", "TASK-2", "TASK-3", "TASK-4", "TASK-5", "TASK-9"):
            with self.subTest(ident=ident):
                self.assertEqual(self.tick(fake, "--issue", ident), 1)
                self.assertEqual(
                    self.said(),
                    [f"pick-none {ident} reason=not a Todo issue assigned to a role account"],
                )
                self.assertEqual((self.start.launches, fake.mutations, self.state), ([], [], ""))


class TickRecords(TickScenario):
    def test_a_usage_blocked_tick_writes_only_its_usage_skips_once_a_day(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "engineer", priority=2, project=PD),
            ]
        )
        for again, five, said in (
            (
                False,
                95,
                [
                    f"usage-skip TASK-{n} runtime=a reason=usage usage=five_hour=95% weekly=10%"
                    for n in (1, 2)
                ],
            ),
            (True, 97, []),
        ):
            with self.subTest(again=again):
                self.api.usages = {"a": reading(five)}
                self.assertEqual(self.tick(fake, again=again), 0)
                self.assertEqual(self.said(), said)
                self.assertEqual((self.start.launches, fake.mutations, self.state), ([], [], ""))

    def test_a_usage_skipped_head_then_a_claim_keeps_every_line(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        self.tick(two())
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=2",
                "pick TASK-1 queue=2",
                "usage-skip TASK-1 runtime=a reason=usage usage=five_hour=95% weekly=10%",
                "pick TASK-2 queue=2",
                "claim TASK-2 role=pm runtime=b",
                "launch TASK-2 exit=0",
            ],
        )

    def test_usage_skips_after_a_claim_skip_end_the_tick_quietly(self):
        self.api.usages = {"a": reading(95)}
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "researcher", priority=2),
            ]
        )
        for sid in "abcd":
            self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=300)
        self.tick(fake)
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=2",
                "claim-skip TASK-1 to=in_review reason=reached 4 attempts",
                "usage-skip TASK-2 runtime=a reason=usage usage=five_hour=95% weekly=10%",
            ],
        )

    def test_an_outage_mid_walk_writes_the_waiting_lines_first(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        gql = failing(FAILURES["timeout"], gql=fake, ops={"issue"})
        self.assertEqual(self.tick(gql), 1)
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=1",
                "pick TASK-1 queue=1",
                f"linear-error op=issue {FIELDS['timeout']}",
            ],
        )
        self.assertEqual((gql.failed[0][0], fake.mutations), (RECHECK, []))

    def test_trim_runs_before_plan(self):
        self.add(kind="start", ident="TASK-8", sid="old", minutes_ago=60 * 24 * 8)
        self.tick(FakeLinear([]))
        self.assertNotIn("TASK-8", self.state)

    def test_trim_failure_continues(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.add(kind="start", ident="TASK-8", sid="old", minutes_ago=60 * 24 * 8)
        with mock.patch.object(os, "replace", side_effect=OSError("disk")):
            self.tick(fake)
        self.assertIn("skip reason=prune failed error=OSError: disk", self.said())
        self.assertEqual(len(self.start.launches), 1)

    def test_the_outers_exit_and_exceptions_never_break_the_tick(self):
        cases = [
            (Start(exits={"TASK-1": 3}), "launch TASK-1 exit=3"),
            (
                Start(raises={"TASK-1": LinearError("linear api error: boom")}),
                "launch-error TASK-1 error=LinearError: linear api error: boom",
            ),
            (
                Start(raises={"TASK-1": RuntimeError("x")}),
                "launch-error TASK-1 error=RuntimeError: x",
            ),
            (
                Start(raises={"TASK-1": AttributeError("start defect")}),
                "launch-error TASK-1 error=AttributeError: start defect",
            ),
            (
                Start(raises={"TASK-1": SystemExit(3)}),
                "launch-error TASK-1 error=SystemExit: 3",
            ),
        ]
        for start, end in cases:
            with self.subTest(end=end):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, start=start), 0)
                self.assertEqual(self.said()[-1], end)
                if "TASK-1" in start.raises:
                    error = start.raises["TASK-1"]
                    record = logged(self.logs)[-1]
                    self.assertIn("in start_run", record["traceback"])
                    self.assertIn(f"{type(error).__name__}: {error}", record["traceback"])
                (launch,) = self.start.launches
                self.assertEqual(
                    self.last_run(), ("start", "TASK-1", launch.sid, "researcher", "a")
                )

    def test_resume_reads_the_session_at_its_recorded_cwd(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.resumable("TASK-1", "a", 60)
        self.api.records[self.sid("a")] = {"cwd": self.tmp}
        self.tick(fake)
        self.assertEqual(self.launched_runs(), [("TASK-1", "resume")])
        self.assertEqual(self.api.info, [("a", self.sid("a"), self.tmp)])

    def test_driver_session_counts_toward_max_runs_and_a_tui_session_does_not(self):
        tui = config.tui_name("engineer", "TASK-1", self.sid("a"))
        for live, ran in (([tui, "agent-pm-engineer-TASK-9"], []), ([tui], [("TASK-1", "new")])):
            with self.subTest(live=live):
                self.tick(
                    FakeLinear([issue("TASK-1", "Todo", "engineer")]),
                    live=live,
                )
                self.assertEqual(self.launched_runs(), ran)


class RunPlanning(Base):
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
        self.add(kind="start", ident="TASK-1", sid="a", minutes_ago=399)
        self.resumable("TASK-3", "c", 100)
        self.resumable("TASK-6", "f", 50)
        for ident, sid in (("TASK-4", "d"), ("TASK-5", "e")):
            for m in (390, 380, 370, 360):
                self.add(kind="start", ident=ident, sid=f"{sid}{m}", minutes_ago=m)
        return fake

    def reset_fixture(self):
        self.write_config(HUMANS + CONFIG)
        self.moved("TASK-1", 700)
        self.add(kind="start", ident="TASK-1", sid="old", minutes_ago=699)
        for m in (650, 600, 550):
            self.add(kind="resume", ident="TASK-1", sid="old", minutes_ago=m)
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.resumable("TASK-1", "new", 100)
        self.add(kind="resume", ident="TASK-1", sid="new", minutes_ago=60)

    def capped(self, hist):
        self.write_config(HUMANS + CONFIG)
        fake = FakeLinear(
            [issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))],
            {"TASK-1": hist},
        )
        for i, sid in enumerate("abcd"):
            self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=400 - i * 50)
        self.touch("d", 200)
        return fake, self.plan(fake)

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
            self.add(kind="resume", ident="TASK-1", sid="sid1", minutes_ago=m)
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
                    issue(
                        "TASK-1",
                        "In Progress",
                        "researcher",
                        priority=1,
                        updated=ago(hours=5),
                    ),
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
            issue(
                "TASK-1",
                "In Progress",
                "researcher",
                priority=1,
                updated=ago(hours=3),
            ),
            issue(
                "TASK-2",
                "In Progress",
                "researcher",
                priority=2,
                updated=ago(hours=3),
            ),
            issue("TASK-3", "In Progress", "researcher", priority=3),
            issue("TASK-4", "In Progress", "pm", priority=4, project=PD),
        )
        for m in (390, 380, 370, 360):
            self.add(kind="start", ident="TASK-1", sid=f"a{m}", minutes_ago=m)
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
        self.add(kind="start", ident="TASK-1", sid="old", minutes_ago=400)
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
        self.add(kind="resume", ident="TASK-1", sid="a", minutes_ago=300)
        self.add(kind="resume", ident="TASK-1", sid="a", minutes_ago=50)
        self.touch("a", 40)
        self.resumable("TASK-2", "b", 200)
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )

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
        self.add(kind="start", ident="TASK-1", sid="a", minutes_ago=10)
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.mutations, [])
        self.lines = []
        self.add(kind="start", ident="TASK-1", sid="a", minutes_ago=40)
        self.assertEqual(self.plan(fake), "new")
        self.assertEqual(
            (fake.issues["TASK-1"]["state"], fake.issues["TASK-1"]["assignee"]),
            ("Todo", who("researcher")),
        )

    def test_no_sid_keeps_two_hour_rule(self):
        self.write_config(HUMANS + CONFIG)
        fake = FakeLinear(
            [
                issue(
                    "TASK-1",
                    "In Progress",
                    "engineer",
                    updated=ago(hours=3),
                    project=PD,
                ),
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
        self.add(kind="start", ident="TASK-1", sid="old", minutes_ago=400)
        self.touch("old", 400)
        self.moved("TASK-1", 220, actor=USER)
        self.assertEqual(self.plan(fake), "new")
        self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")

    def test_tolerance(self):
        for start, want in ((100.05, "resume"), (106, "")):
            self.lines, self.hist = [], {}
            fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
            self.moved("TASK-1", 100)
            self.add(kind="start", ident="TASK-1", sid="a", minutes_ago=start)
            self.touch("a", 40)
            self.assertEqual(self.plan(fake).split(" ")[0], want)

    def test_no_move_in_history_is_current(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.moved("TASK-1", 500, state=STATES["Todo"], actor=USER)
        self.add(kind="start", ident="TASK-1", sid="a", minutes_ago=100)
        self.touch("a", 40)
        self.assertEqual(
            self.plan(fake), "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"
        )

    def test_no_current_sid_never_capped(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher", updated=ago(hours=1)))
        for m in (600, 550, 500, 450):
            self.add(kind="start", ident="TASK-1", sid=f"s{m}", minutes_ago=m)
        self.moved("TASK-1", 60, actor=USER)
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.mutations, [])

    def test_launch_failures_end_in_review(self):
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        for n in range(4):
            self.moved("TASK-1", 41)
            self.add(kind="start", ident="TASK-1", sid=f"s{n}", minutes_ago=40)
            if n < 3:
                self.assertEqual(self.plan(fake), "new")
                self.assertEqual(fake.issues["TASK-1"]["state"], "Todo")
                self.assertEqual(
                    self.claim(fake), "TASK-1 https://linear.app/x/TASK-1 Deep Research"
                )
        self.assertEqual(self.plan(fake), "")
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")
        self.assertEqual(fake.issues["TASK-1"]["comments"][-1], router.CAP_COMMENT)

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
                fake = self.fake(
                    issue("TASK-1", "In Progress", "researcher", priority=2),
                    *others,
                )
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
                    [
                        issue(
                            "TASK-1",
                            "In Progress",
                            "researcher",
                            updated=ago(minutes=40),
                        )
                    ]
                )
                self.lines = []
                for i, sid in enumerate("abcd"):
                    self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=400 - i * 50)
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
        with self.assertRaises(LinearError) as e:
            self.plan(FakeLinear([]))
        self.assertIn("nobody@x.com", str(e.exception))

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
            [issue("TASK-1", "In Progress", "researcher", updated=ago(hours=3))],
            "In Review",
        )
        self.assertEqual(self.plan(fake), "")
        self.assertEqual((fake.issues["TASK-1"]["state"], fake.mutations), ("In Review", []))
        self.assertNotIn("comments", fake.issues["TASK-1"])
        self.assertEqual(writes(fake), [("read", "TASK-1")])
        self.assertIn(f"move-skip TASK-1 step=recover state={STATES['In Review']}", self.said())


class HistoryReads(Base):
    def test_one_history_fetch_per_issue(self):
        fake = FakeLinear([issue("TASK-1", "In Progress", "researcher")])
        for sid in "abcd":
            self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=300)
        queries = []
        self.plan(lambda q, **v: queries.append(q) or fake(q, **v))
        self.assertEqual(sum("history" in q for q in queries), 1)
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Review")


class StaleRunRecovery(Base):
    """FR-47: liveness and resumability are the Driver's session status; what it cannot say changes
    nothing.
    """

    def test_a_session_active_within_live_is_not_resumed(self):
        for minutes, out in (
            (10, ""),
            (29, ""),
            (31, "resume TASK-1 a https://linear.app/x/TASK-1 Deep Research"),
        ):
            with self.subTest(minutes=minutes):
                self.lines, self.hist = [], {}
                fake = self.fake(
                    issue(
                        "TASK-1",
                        "In Progress",
                        "researcher",
                        updated=ago(hours=3),
                    )
                )
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
                self.add(kind="start", ident="TASK-1", sid="a", minutes_ago=minutes)
                self.api.statuses[self.sid("a")] = SessionStatus("no", None, "")
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
            SessionStatus("unknown", None, ""),
            DriverError("boom"),
            ConfigError("no runtime a"),
        )
        for st in statuses:
            with self.subTest(st=st):
                self.lines, self.hist = [], {}
                fake = self.fake(
                    issue(
                        "TASK-1",
                        "In Progress",
                        "researcher",
                        updated=ago(hours=3),
                    )
                )
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
        self.api.statuses[self.sid("a")] = DriverError("boom")

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


class ClaimRechecks(Base):
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
            self.claim(fake, gate=True),
            "TASK-1 https://linear.app/x/TASK-1 Deep Research",
        )
        taken, task, role, runtime = self.taken
        self.assertEqual((taken["assignee"], task, role, runtime), (who("pm"), None, "pm", "b"))
        self.assertEqual(self.said(), ["pick TASK-1 queue=1", "claim TASK-1 role=pm runtime=b"])
        self.assertEqual(self.api.usage_calls, ["b"])
        a = router.begin(
            self.runs, mode="new", issue=taken, sid="s1", role=role, task=task, runtime=runtime
        )
        self.assertEqual((a.assignee, a.runtime), (ROLE["pm"], "b"))

    def test_the_task_label_is_checked_against_the_new_role(self):
        self.write_config(HUMANS + CONFIG + TASK_LABELS)
        fake = Changed(
            [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    labels=[label("Light Research", LIGHT)],
                )
            ],
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
        a = router.begin(
            self.runs,
            mode="new",
            issue=self.taken[0],
            sid="s1",
            role="researcher",
            task=None,
            runtime="a",
        )
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


class LiveLimits(Base):
    """FR-60: a live limit row blocks its runtime with no probe; any other limit row changes
    nothing.
    """

    def test_a_live_limit_row_skips_its_runtime_without_a_probe(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        self.limit("a", at)
        fake = two()
        self.assertEqual(
            self.claim(fake, gate=True),
            f"TASK-2 https://linear.app/x/TASK-2 {PD}",
        )
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
                    self.claim(fake, gate=True),
                    "TASK-1 https://linear.app/x/TASK-1 Deep Research",
                )
                self.assertEqual(self.api.usage_calls, ["a"])

    def test_limit_rows_are_no_attempts(self):
        for sid in "abc":
            self.add(kind="start", ident="TASK-1", sid=sid, minutes_ago=300)
        for _ in range(4):
            self.limit("b", (NOW + timedelta(hours=1)).isoformat(), ident="TASK-1")
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.claim(fake), "TASK-1 https://linear.app/x/TASK-1 Deep Research")


class SessionInventory(Base):
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
            router.live_sessions(["researcher", "pm", "pm-lead", "engineer"], driver=self.api),
            {
                "researcher": ["TASK-1"],
                "pm": ["TASK-6"],
                "pm-lead": ["TASK-4"],
                "engineer": ["TASK-10", "TASK-2"],
            },
        )
        self.assertEqual(self.api.order, SESSIONS)

    def test_unknown_sessions_are_none(self):
        self.api.live = None
        self.assertIsNone(router.live_sessions(["researcher", "engineer"], driver=self.api))


class ScheduledRuns(TickScenario):
    def test_hours_boundaries(self):
        for hour, ran in ((0, False), (1, True), (6, True), (7, False), (12, False), (23, False)):
            with self.subTest(hour=hour):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, hour=hour), 0)
                self.assertEqual(len(self.start.launches), int(ran))
                self.assertEqual(self.said() == [], not ran)
                self.assertEqual(self.api.order == [], not ran)

    def test_launchd_path_replaced(self):
        self.tick(FakeLinear([]))
        self.assertEqual(self.path, config.path())
        self.assertIn("/opt/homebrew/bin", self.path)

    def test_now_skips_hours_and_starts(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.tick(fake, "--now", hour=12)
        (launch,) = self.start.launches
        self.assertEqual(
            launch,
            argparse.Namespace(
                issue="TASK-1",
                project=IDS[DR],
                assignee=ROLE["researcher"],
                sid=launch.sid,
                task=None,
                mode="new",
                runtime="a",
            ),
        )
        self.assertRegex(launch.sid, config.UUID_RE)
        self.assertEqual(self.last_run(), ("start", "TASK-1", launch.sid, "researcher", "a"))
        self.assertEqual(fake.issues["TASK-1"]["state"], "In Progress")

    def test_begin_appends_one_json_line_to_runs_jsonl(self):
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        stamp = datetime.fromisoformat("2026-10-09T00:26:33-04:00").astimezone().isoformat()
        self.tick(fake, "--now", hour=12, now=datetime.fromisoformat(stamp))
        (launch,) = self.start.launches
        self.assertEqual(
            self.state,
            f'{{"ts": "{stamp}", "kind": "start", "issue": "TASK-1", '
            f'"sid": "{launch.sid}", "role": "researcher", "runtime": "a"}}\n',
        )
        self.assertEqual(
            sorted(os.listdir(os.path.dirname(self.runs))),
            ["orchestrator.jsonl", "router.lock", "runs.jsonl"],
        )

    def test_start_records_write_time_after_a_slow_tick(self):
        written = NOW + timedelta(minutes=5)
        times = iter((NOW, written))
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.tick(fake, clock=lambda: next(times)), 0)
        self.assertEqual(runs.parse_log(self.runs)[-1].ts, written)

    def test_resume_records_write_time_after_a_slow_issue_lookup(self):
        written = NOW + timedelta(minutes=5)
        times = iter((NOW, written))
        fake = self.fake(issue("TASK-1", "In Progress", "researcher"))
        self.resumable("TASK-1", "a", 60)
        self.assertEqual(self.tick(fake, "--issue", "TASK-1", clock=lambda: next(times)), 0)
        entry = runs.parse_log(self.runs)[-1]
        self.assertEqual((entry.kind, entry.ts), ("resume", written))

    def test_an_idle_tick_writes_no_line(self):
        cases = [
            (12, [], [issue("TASK-1", "Todo", "researcher")]),
            (2, FULL, [issue("TASK-1", "Todo", "researcher")]),
            (2, ["agent-pm-engineer-TASK-2"], [issue("TASK-2", "In Progress", "engineer")]),
        ]
        for hour, live, listed in cases:
            with self.subTest(hour=hour, live=live):
                self.assertEqual(self.tick(FakeLinear(listed), hour=hour, live=live), 0)
                self.assertEqual((self.said(), self.err), ([], ""))
        self.assertFalse(os.path.exists(os.path.join(self.logs, "orchestrator.jsonl")))
        blocked = [issue("TASK-1", "Todo", "researcher", inverse=[blocker("TASK-7")])]
        self.tick(FakeLinear(blocked))
        self.assertEqual(self.said(), ['blocked TASK-1 by=["TASK-7"]'])
        self.tick(FakeLinear(blocked), again=True)
        self.assertEqual(self.said(), [])

    def test_unknown_agent_runs_change_nothing(self):
        self.add(kind="start", ident="TASK-8", sid="old", minutes_ago=60 * 24 * 8)
        before = self.before()
        for argv in ((), ("--dry-run",), ("--now",)):
            with self.subTest(argv=argv):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv, live=None), 0)
                self.assertEqual(self.said(), ["skip reason=agent runs unknown"])
                self.assertEqual(
                    (fake.queries, self.start.launches, self.api.usage_calls, self.state),
                    ([], [], [], before),
                )

    def test_all_full_skips_before_linear(self):
        self.add(kind="start", ident="TASK-8", sid="old", minutes_ago=60 * 24 * 8)
        for cfg, live in ((CONFIG, FULL), (CONFIG_ENG2, [*FULL, "agent-pm-engineer-TASK-9"])):
            self.write_config(cfg)
            counts = json.dumps(
                {
                    r: f"{n}/{n}"
                    for r, n in (("engineer", len(live) - 2), ("pm", 1), ("researcher", 1))
                }
            )
            for argv in ((), ("--dry-run",), ("--now",)):
                fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
                self.assertEqual(self.tick(fake, *argv, live=live), 0)
                self.assertEqual(
                    self.said(),
                    [f"skip reason=all roles full live={counts}"] if "--dry-run" in argv else [],
                    (cfg, argv),
                )
                self.assertEqual(
                    (fake.queries, self.api.order, self.api.usage_calls), ([], SESSIONS, []), argv
                )
                self.assertIn("TASK-8", self.state, argv)

    def test_partly_full_role_gets_a_launch(self):
        self.write_config(CONFIG_ENG2)
        fake = self.fake(
            issue("TASK-1", "In Progress", "engineer", priority=1, project=PD),
            issue("TASK-2", "Todo", "engineer", priority=2, project=PD),
            issue("TASK-3", "Todo", "engineer", priority=3, project=PD),
        )
        self.resumable("TASK-1", "a", 60)
        self.tick(fake, live=["agent-pm-engineer-TASK-1"])
        self.assertEqual(
            self.said(),
            [
                'plan mode=new queue=2 live={"engineer": "1/2"}',
                "pick TASK-2 queue=2",
                "claim TASK-2 role=engineer runtime=a",
                "launch TASK-2 exit=0",
            ],
        )
        self.assertEqual(self.launched_runs(), [("TASK-2", "new")])
        self.assertEqual(
            {i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
            {
                "TASK-1": ("In Progress", None),
                "TASK-2": ("In Progress", None),
                "TASK-3": ("Todo", None),
            },
        )

    def test_todo_issue_with_live_session_not_claimed(self):
        self.write_config(CONFIG_ENG2)
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "engineer", priority=1, project=PD),
                issue("TASK-2", "Todo", "engineer", priority=2, project=PD),
            ]
        )
        self.tick(fake, live=["agent-pm-engineer-TASK-1"])
        self.assertEqual(
            self.said(),
            [
                'plan mode=new queue=1 live={"engineer": "1/2"}',
                "pick TASK-2 queue=1",
                "claim TASK-2 role=engineer runtime=a",
                "launch TASK-2 exit=0",
            ],
        )
        self.assertEqual(
            {i: t["state"] for i, t in fake.issues.items()},
            {"TASK-1": "Todo", "TASK-2": "In Progress"},
        )

    def test_full_role_todo_not_claimed_and_non_live_in_progress_recovered(self):
        fake = self.fake(
            issue(
                "TASK-1",
                "In Progress",
                "engineer",
                priority=1,
                updated=ago(hours=3),
            ),
            issue("TASK-2", "In Progress", "engineer", updated=ago(hours=3)),
            issue("TASK-3", "In Progress", "engineer"),
            issue("TASK-4", "Todo", "engineer", priority=1),
            issue("TASK-5", "Todo", "pm", priority=3, project=PD),
            issue("TASK-6", "Todo", "engineer", priority=1),
        )
        for ident in ("TASK-1", "TASK-3", "TASK-4"):
            for m in (390, 380, 370, 360):
                self.add(kind="start", ident=ident, sid=f"{ident}-{m}", minutes_ago=m)
        self.tick(fake, live=["agent-pm-engineer-TASK-1"])
        self.assertEqual(
            self.said(),
            [
                "recover TASK-3 to=in_review reason=reached 4 attempts",
                f"recover TASK-2 to=todo reason=no session updated={ago(hours=3)}",
                'plan mode=new queue=1 live={"engineer": "1/1"}',
                "pick TASK-5 queue=1",
                "claim TASK-5 role=pm runtime=b",
                "launch TASK-5 exit=0",
            ],
        )
        self.assertEqual(
            {i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
            {
                "TASK-1": ("In Progress", None),
                "TASK-2": ("Todo", [router.INTERRUPTED]),
                "TASK-3": ("In Review", [router.CAP_COMMENT]),
                "TASK-4": ("Todo", None),
                "TASK-5": ("In Progress", None),
                "TASK-6": ("Todo", None),
            },
        )
        self.assertEqual(self.launched(), "TASK-5")
        self.assertNotIn("TASK-1", [v["i"] for q, v in fake.queries if "history" in q])
        flts = [v["f"] for q, v in fake.queries if "issues(filter" in q]
        self.assertEqual(
            {tuple(sorted(f["assignee"]["id"]["in"])) for f in flts},
            {tuple(sorted(f"u-{r}" for r in ROLE))},
        )

    def test_full_role_resume_not_launched(self):
        fake = self.fake(
            issue("TASK-1", "In Progress", "pm", priority=1, project=PD),
            issue("TASK-2", "In Progress", "researcher", priority=3),
            issue("TASK-3", "Todo", "engineer", priority=1),
        )
        self.resumable("TASK-1", "a", 120, "pm")
        self.resumable("TASK-2", "b", 60)
        self.tick(fake, live=["agent-pm-pm-TASK-9"])
        self.assertEqual(self.said()[0], 'plan TASK-2 mode=resume sid=b live={"pm": "1/1"}')
        self.assertEqual(self.launched_runs(), [("TASK-2", "resume")])
        self.assertEqual(
            {i: (t["state"], t.get("comments")) for i, t in fake.issues.items()},
            {
                "TASK-1": ("In Progress", None),
                "TASK-2": ("In Progress", None),
                "TASK-3": ("Todo", None),
            },
        )
        self.assertEqual(self.last_run(), ("resume", "TASK-2", "b", "researcher", "a"))
        self.assertEqual(self.api.usage_calls, ["a"])

    def test_nothing_to_do_with_full_role(self):
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "engineer"),
                issue("TASK-2", "In Progress", "engineer", updated=ago(hours=3)),
            ]
        )
        self.tick(fake, live=["agent-pm-engineer-TASK-2"])
        self.assertEqual(self.said(), [])
        self.assertEqual((self.api.order, self.api.usage_calls), (SESSIONS, []))
        self.assertEqual(
            [v["e"] for q, v in fake.queries if "users(filter" in q],
            [ROLE["researcher"], ROLE["pm"], ROLE["engineer"]],
        )
        self.assertEqual(fake.mutations, [])

    def test_one_run_per_tick_resume_first(self):
        self.write_config(CONFIG_ENG2)
        fake = self.fake(
            issue("TASK-1", "In Progress", "researcher", priority=3),
            issue("TASK-8", "In Progress", "engineer", priority=4, project=PD),
            issue("TASK-2", "Todo", "researcher", priority=1),
            issue("TASK-3", "Todo", "pm", priority=2, project=PD),
            issue("TASK-4", "Todo", "engineer", priority=2, project=PD),
        )
        self.resumable("TASK-1", "a", 60)
        self.resumable("TASK-8", "h", 70, "engineer")
        self.tick(fake)
        self.assertEqual(self.said(), ["plan TASK-1 mode=resume sid=a", "launch TASK-1 exit=0"])
        self.assertEqual(self.launched_runs(), [("TASK-1", "resume")])
        self.assertEqual(
            {i: t["state"] for i, t in fake.issues.items()},
            {
                "TASK-1": "In Progress",
                "TASK-8": "In Progress",
                "TASK-2": "Todo",
                "TASK-3": "Todo",
                "TASK-4": "Todo",
            },
        )
        self.assertEqual((self.api.usage_calls, self.api.order), (["a"], SESSIONS))

    def test_one_claim_per_tick(self):
        self.write_config(CONFIG_ENG2)
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-2", "Todo", "pm", priority=2, project=PD),
                issue("TASK-3", "Todo", "engineer", priority=2, project=PD),
            ]
        )
        self.tick(fake)
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=3",
                "pick TASK-1 queue=3",
                "claim TASK-1 role=researcher runtime=a",
                "launch TASK-1 exit=0",
            ],
        )
        self.assertEqual((self.api.order, self.api.usage_calls), (SESSIONS, ["a"]))
        self.assertEqual(
            {i: t["state"] for i, t in fake.issues.items()},
            {"TASK-1": "In Progress", "TASK-2": "Todo", "TASK-3": "Todo"},
        )

    def test_dry_run_plans_one_run(self):
        self.write_config(CONFIG_ENG2)
        fake = self.fake(
            issue("TASK-3", "Todo", "pm", priority=1, project=PD),
            issue("TASK-4", "Todo", "pm", priority=3, project=PD),
            issue("TASK-5", "Todo", "engineer", priority=2, project=PD),
            issue("TASK-6", "Todo", "engineer", priority=3, project=PD),
        )
        for sid in "wxyz":
            self.add(kind="start", ident="TASK-3", sid=sid, minutes_ago=300)
        before = self.before()
        self.assertEqual(self.tick(fake, "--dry-run"), 0)
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=4",
                "claim-skip TASK-3 to=in_review reason=reached 4 attempts",
                "pick TASK-5 queue=4",
                "usage runtime=a usage=five_hour=10% weekly=10% allowed=true",
            ],
        )
        self.assertEqual((self.api.order, self.api.usage_calls), (SESSIONS, ["a"]))
        self.assertEqual((fake.mutations, self.state, self.start.launches), ([], before, []))
        self.assertNotIn(RECHECK, [q for q, _ in fake.queries])

    def test_dry_run_usage_blocked(self):
        self.api.usages = {"a": reading(95)}
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.tick(fake, "--dry-run"), 0)
        why = "usage=five_hour=95% weekly=10%"
        self.assertEqual(
            self.said(),
            [
                "plan mode=new queue=1",
                "pick TASK-1 queue=1",
                f"usage runtime=a {why} allowed=false",
                f"usage-skip TASK-1 runtime=a reason=usage {why}",
                "pick-none reason=nothing claimable",
                "skip reason=nothing claimed",
            ],
        )
        self.assertEqual((fake.mutations, self.start.launches), ([], []))

    def test_dry_run(self):
        self.add(kind="start", ident="TASK-8", sid="old", minutes_ago=60 * 24 * 8)
        fake = FakeLinear(
            [issue("TASK-1", "Todo", "engineer"), issue("TASK-2", "Todo", "researcher")]
        )
        self.tick(
            fake,
            "--dry-run",
            hour=12,
            live=["agent-pm-pm-TASK-5", "agent-pm-engineer-TASK-6"],
        )
        self.assertEqual(
            self.said(),
            [
                "skip reason=outside hours",
                'plan mode=new queue=1 live={"engineer": "1/1", "pm": "1/1"}',
                "pick TASK-2 queue=1",
                "usage runtime=a usage=five_hour=10% weekly=10% allowed=true",
            ],
        )
        self.assertEqual(self.api.usage_calls, ["a"])
        self.assertEqual((self.start.launches, fake.mutations), ([], []))
        self.assertIn("TASK-8", self.state)

    def test_all_todo_blocked_reads_no_usage(self):
        def issues():
            return [
                issue(
                    "TASK-1",
                    "Todo",
                    "researcher",
                    inverse=[blocker("TASK-2", "unstarted")],
                ),
                issue(
                    "TASK-2",
                    "Todo",
                    "pm",
                    project=PD,
                    inverse=[blocker("TASK-1", "unstarted")],
                ),
                issue(
                    "TASK-3",
                    "Todo",
                    "engineer",
                    inverse=[blocker("TASK-9", "started")],
                ),
            ]

        blocked = [
            'blocked TASK-1 by=["TASK-2"]',
            'blocked TASK-2 by=["TASK-1"]',
            'blocked TASK-3 by=["TASK-9"]',
        ]
        for argv in ((), ("--dry-run",)):
            with self.subTest(argv=argv):
                fake = FakeLinear(issues())
                self.assertEqual(self.tick(fake, *argv), 0)
                self.assertEqual(self.said(), blocked)
                self.assertEqual(
                    (self.api.usage_calls, self.start.launches, fake.mutations), ([], [], [])
                )
                self.assertEqual(len(fake.reads()), 1)

    def test_ready_tick_reads_todo_once(self):
        for argv in ((), ("--issue", "TASK-2")):
            fake = FakeLinear(
                [
                    issue(
                        "TASK-1",
                        "Todo",
                        "researcher",
                        priority=1,
                        inverse=[blocker("TASK-7")],
                    ),
                    issue("TASK-2", "Todo", "researcher", priority=2),
                    issue("TASK-3", "Todo", "pm", priority=3, project=PD),
                ]
            )
            self.assertEqual(self.tick(fake, *argv), 0, argv)
            self.assertEqual(self.launched_runs(), [("TASK-2", "new")], argv)
            self.assertEqual(len(fake.reads()), 1, argv)
            self.assertEqual("plan mode=new queue=2" in self.said(), not argv, argv)
            self.assertEqual(self.said().count('blocked TASK-1 by=["TASK-7"]'), 1, argv)
