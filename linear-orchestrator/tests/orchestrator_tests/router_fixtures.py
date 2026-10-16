"""Shared orchestrator scenario fixtures for unit and sandboxed integration contracts."""

import functools
import io
import json
import os
import re
import tempfile
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from unittest import mock

from agent_pm.delegate import Spec as DelegateSpec
from agent_pm.linear_orchestrator.engine import (
    config,
    inputs,
    ops,
    related,
    router,
    runs,
)
from agent_pm.linear_orchestrator.engine import (
    gate as quota,
)
from agent_pm.linear_orchestrator.engine.types import LinearError, Spec
from agent_pm.session import SessionStatus
from orchestrator_tests.unit import run_fixtures
from orchestrator_tests.unit.board_ids import ACCOUNTS, HEADER, TEAM, team_node
from orchestrator_tests.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_tests.unit.board_ids import role as role_table
from orchestrator_tests.unit.clock import logs_at
from orchestrator_tests.unit.delegate_fixture import provider
from orchestrator_tests.unit.fake_api import FakeApi
from orchestrator_tests.unit.run_fixtures import (
    ENGINEER,
    HARNESS,
    ID,
    PROJECT,
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


CLIENTS = {"researcher": "a", "pm": "b", "engineer": "a"}


OVERLAY = '[core]\nclient = "a"\n\n[core.roles.pm]\nclient = "b"\n'


CONFIG = (
    HEADER
    + role_table("researcher", 'next = "pm"')
    + role_table("pm", 'next = "engineer"')
    + role_table("engineer")
    + OVERLAY
)


LIGHT, DEEP, STRAY, ORPHAN_LABEL = (
    f"00000000-0000-4000-8000-0000000000{n}" for n in (21, 22, 23, 24)
)


RECHECK = (
    "query($i: String!) { issue(id: $i) { state { id } assignee { id email } project { id name } "
    "labels { nodes { id name parent { id } } } } }"
)


NO = SessionStatus("no", None, "")


RESUME = ["ts", "kind", "issue", "sid", "role", "client", "delegate_kind"]


START = [*RESUME, "uuid", "project", "target", "key"]


def ago(**kw):
    return (NOW - timedelta(**kw)).isoformat().replace("+00:00", "Z")


def who(role):
    return None if role is None else {"id": f"u-{role}", "email": ROLE.get(role, f"{role}@x.com")}


def op(query):
    return re.search(r"\{ (\w+)\(", query).group(1)


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


LISTING = ("gh", "api", "repos/ophis/private_docs/contents/Research?ref=main")


DESIGN_LISTING = ("gh", "api", "repos/ophis/private_docs/contents/Product%20Design?ref=main")


PRS = ("gh", "search", "prs")


# a first claim's related search: no docs, no PRs
SEARCHED = [(LISTING, ok("[]")), (DESIGN_LISTING, ok("[]")), (PRS, ok("[]"))]


ENG_RUN = [
    (("gh", "api", "repos/ophis/agent-pm"), repo_info("Ophis/Agent-PM")),
    (LS_REMOTE[:3], ok()),
    *SEARCHED,
]


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


class FakeLinear:
    def __init__(self, issues, history=None):
        self.issues = {i["identifier"]: i for i in issues}
        self.history = {} if history is None else history
        self.mutations, self.queries, self.services = [], [], []
        self.todo_error = False
        self.unreadable = set()
        self.refuse = set()
        self.ids = {}  # an issue's uuid → its identifier, for write-back's calls
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
        if query in (related.Q_SEARCH, related.Q_SEARCH_IDS, related.Q_IDS):
            return {"searchIssues": {"nodes": []}, "issues": {"nodes": []}}
        if query == ops.Q_TEAM:
            return {"teams": {"nodes": [team_node()]}}
        if query == ops.Q_TASK_GROUP:
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
        if "attachments" in query:
            return {"issue": {"attachments": {"nodes": []}}}
        if "history" in query:
            return {"issue": {"history": {"nodes": self.history.get(v["i"], [])}}}
        if query == ops.Q_ISSUE_STATE:
            issue = self.issues[self.ids.get(v["i"], v["i"])]
            return {"issue": {"state": {"id": STATES[issue["state"]]}}}
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
        issue = self.issues[self.ids.get(v["i"], v["i"])]
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


class Base(run_fixtures.Base):
    def setUp(self):
        super().setUp()
        self.missing, self.keychain_calls = set(), []
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

    def row(self, kind, ident, sid, role="researcher", client=None, **ago):
        return json.dumps(
            {
                "ts": (NOW - timedelta(**ago)).astimezone(WEST).isoformat(timespec="seconds"),
                "kind": kind,
                "issue": ident,
                "sid": self.sid(sid),
                "role": role,
                "client": client or CLIENTS.get(role, "a"),
                "delegate_kind": "harness",
            }
        )

    def add(self, *, kind, ident, sid, minutes_ago, role="researcher", client=None):
        self.lines.append(
            self.row(
                kind,
                ident,
                sid,
                role,
                client,
                minutes=minutes_ago,
            )
        )

    def limit(self, client, resets_at, ident="TASK-9"):
        self.lines.append(
            json.dumps(
                {
                    "ts": NOW.astimezone(WEST).isoformat(timespec="seconds"),
                    "kind": "limit",
                    "issue": ident,
                    "sid": self.sid("limited"),
                    "client": client,
                    "resets_at": resets_at,
                    "delegate_kind": "harness",
                }
            )
        )

    def touch(self, sid, minutes_ago):
        # the session's transcript, last written minutes_ago
        at = (NOW - timedelta(minutes=minutes_ago)).timestamp()
        entry = next(
            (
                json.loads(line)
                for line in reversed(self.lines)
                if json.loads(line).get("sid") == self.sid(sid)
            ),
            None,
        )
        if entry is not None:
            self.api.records[self.sid(sid)] = {
                "cwd": os.path.join(self.work, "work", entry["issue"])
            }
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

    def keychain(self, service):
        self.keychain_calls.append(service)
        return service not in self.missing

    def cfg(self):
        cfg = config.load(path=self.config_path)
        return replace(cfg, delegates={**cfg.delegates, cfg.delegate_kind: provider(self.api, cfg)})

    def board(self, fake, use, *, dry=False, gate=False, again=False):
        self.write_runs()
        cfg = self.cfg()
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(again=again):
            out = use(
                router.Board(
                    functools.partial(fake, service=HARNESS),
                    runs.parse_log(self.runs),
                    cfg,
                    now=NOW,
                    dry=dry,
                    keychain=self.keychain,
                    gate=quota.Gate(self.runs, NOW, dry=dry, cfg=cfg) if gate else None,
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
    """The injected start: records each launch Spec; per issue an exit code (`exits`) or an
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
        env=None,
    ):
        # outer: main's run and keychain; main then starts through its own outer
        # env: the caller's variables over PATH; a launcher's events file and sid only from env
        self.api.live = None if live is None else list(live)
        recorded = {json.loads(line)["issue"] for line in self.lines}
        for name in live or ():
            found = re.fullmatch(r"agent-pm-([a-z][a-z0-9-]*)-([A-Z][A-Z0-9]*-\d+)", name)
            if found and found[2] not in recorded:
                self.add(
                    kind="start", ident=found[2], sid=f"live:{name}", minutes_ago=10, role=found[1]
                )
                recorded.add(found[2])
        if live is None and not self.lines:
            self.add(kind="start", ident="TASK-0", sid="unknown", minutes_ago=10)
        try:
            delegate_cls = self.cfg().delegate_cls
        except config.ConfigError:
            delegate_cls = None

        self.api.order, self.api.usage_calls, self.api.info = [], [], []
        self.start = None if outer else start or Start()
        self.write_runs()
        err, out = Stderr(tty=tty), io.StringIO()
        with (
            redirect_stderr(err),
            redirect_stdout(out),
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}),
            self.log_dir(again=again),
        ):
            for key in (router.EVENTS_ENV, router.SID_ENV):
                os.environ.pop(key, None)
            os.environ.update(env or {})
            rc = router.main(
                list(argv),
                now=clock or (lambda: now),
                config_path=self.config_path,
                hour=hour,
                gql=fake,
                delegate_cls=delegate_cls,
                start=self.start,
                **{"keychain": self.keychain, **(outer or {})},
            )
            self.path = os.environ["PATH"]
        self.raw, self.err = err.getvalue(), self.unmap(err.getvalue())
        self.out = out.getvalue()
        with open(self.runs) as f:
            self.state = self.unmap(f.read())
        return rc

    def before(self):
        return self.unmap("".join(f"{line}\n" for line in self.lines))

    def last_run(self):
        """runs.jsonl's last row, a start or resume row."""
        entry = json.loads(self.state.splitlines()[-1])
        self.assertEqual(list(entry), START if entry["kind"] == "start" else RESUME)
        return entry["kind"], entry["issue"], entry["sid"], entry["role"], entry["client"]

    def raw_runs(self):
        with open(self.runs, encoding="utf-8") as f:
            return f.read()

    def launched_run(self):
        """The run the tick handed to its start, as last_run's row would show it."""
        a = self.start.launches[-1]
        role = config.role_for(self.cfg().roles, a.assignee)
        return "start" if a.mode == "new" else "resume", a.issue, self.unmap(a.sid), role, a.client

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
        self.api.records[SID] = {"cwd": self.rd}

    def outer(self, *, assignee=ENGINEER, task=None, mode="new", ident=ID, sid=SID, client="a"):
        """router.outer on a Spec; a resume's run is recorded first, as Recover finds it."""
        if mode == "resume" and sid not in {e.sid for e in runs.parse_log(self.runs)}:
            role = config.role_for(self.cfg().roles, assignee) or "engineer"
            runs.note(
                self.runs,
                "start",
                issue=ident,
                sid=sid,
                role=role,
                client=client,
                delegate_kind="harness",
            )
        a = Spec(
            uuid=UUID,
            issue=ident,
            project=PROJECT,
            assignee=assignee,
            sid=sid,
            task=task,
            mode=mode,
            client=client,
        )
        cfg = self.cfg()
        err = io.StringIO()
        with redirect_stderr(err), self.log_dir(again=True):
            rc = router.outer(
                a,
                cfg,
                gql=functools.partial(self.gql, service=HARNESS),
                run=self.run_,
                keychain=self.keychain,
                now=lambda: NOW,
            )
        self.err = err.getvalue()
        return rc

    def detached(self, role="engineer"):
        """The Delegate Spec of the last detached run, after its role, workdir and options."""
        found, spec, options = self.api.detached[-1]
        self.assertEqual((found, spec.workdir), (role, self.rd))
        self.assertEqual(
            options,
            {
                "display": None,
                "callback": ["agent-pm-linear", "writeback", "--issue", ID, "--workdir", self.rd],
                "overlays": (self.cfg().core,),
            },
        )
        return spec

    def expected(
        self, role="engineer", *, task=None, sid=SID, client="a", resume=False, text=INPUT
    ):
        return DelegateSpec(
            text,
            self.rd,
            task=task,
            client=client,
            sid=sid,
            resume=resume,
            name=f"{role}-{ID}-{sid[:8]}",
        )

    def input(self, **kw):
        return self.detached(**kw).input
