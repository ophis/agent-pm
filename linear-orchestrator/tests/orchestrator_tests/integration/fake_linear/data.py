import hashlib
from datetime import UTC, datetime

from agent_pm.linear_orchestrator.engine import config, promote
from agent_pm.linear_orchestrator.engine.prune import types as prune
from agent_pm.linear_orchestrator.engine.routing import types as routing
from agent_pm.linear_orchestrator.linear import issues, linear, sessions, writeback
from orchestrator_tests.unit.board_ids import STATES

KEY = "TASK"


BACKLOG = "00000000-0000-4000-8000-000000000017"


IDS = {**STATES, "backlog": BACKLOG}


TYPES = {
    "todo": "unstarted",
    "in_progress": "started",
    "in_review": "started",
    "handoff": "started",
    "done": "completed",
    "canceled": "canceled",
    "backlog": "backlog",
}


NAMES = {i: k for k, i in IDS.items()}


TITLES = {**config.STATES, "backlog": "Backlog"}


KINDS = ("error", "unsuccessful", "503", "hang")


MODULES = {
    "linear": linear,
    "issues": issues,
    "sessions": sessions,
    "writeback": writeback,
    "promote": promote,
    "prune": prune,
    "router": routing,
}


BUILT = {
    "router.issues": routing.q_issues(),
    "router.issues+relations": routing.q_issues(routing.RELATIONS),
}


CREATE = {"id", "teamId", "projectId", "assigneeId", "stateId", "priority", "title", "description"}


COMPARATORS = {
    "eq": lambda x, a: x == a,
    "in": lambda x, a: x in a,
    "null": lambda x, a: (x is None) == a,
}


def stamp(t=None):
    if t is None:
        t = datetime.now(UTC)
    elif isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    t = (t if t.tzinfo else t.replace(tzinfo=UTC)).astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def credential_key(service):
    return "lin_api_" + hashlib.sha256(service.encode()).hexdigest()[:32]


def error_payload(message):
    return {"data": None, "errors": [{"message": message}]}


def newest(nodes):
    return sorted(reversed(nodes), key=lambda n: n["createdAt"], reverse=True)
