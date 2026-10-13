import argparse
import contextlib
import fcntl
import os
import re
import sys
from datetime import UTC, datetime, timedelta, timezone

from agent_pm.linear_orchestrator.engine import inputs
from agent_pm.linear_orchestrator.linear import linear
from agent_pm.session import SessionStatus, Window
from agent_pm.session import Usage as SessionUsage
from orchestrator_test_helpers.unit import run_fixtures
from orchestrator_test_helpers.unit.board_ids import ACCOUNTS, HEADER, TASK_GROUP
from orchestrator_test_helpers.unit.board_ids import STATES as IDS_BY_KEY
from orchestrator_test_helpers.unit.board_ids import role as role_table
from orchestrator_test_helpers.unit.run_fixtures import ENGINEER, ID, PROJECT, SID, UUID
from orchestrator_test_helpers.unit.target_support import api as repo_info
from orchestrator_test_helpers.unit.target_support import ok

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


def outer_args(ident, project, assignee, sid, task, mode, runtime):
    return argparse.Namespace(
        issue=ident,
        project=project,
        assignee=assignee,
        sid=sid,
        task=task,
        mode=mode,
        runtime=runtime,
    )


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
