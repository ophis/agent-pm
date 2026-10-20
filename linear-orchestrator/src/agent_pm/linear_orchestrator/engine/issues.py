"""The Handoff's reader-side helpers: brief, is_user and build_cutoff."""

import re
from functools import cache
from types import SimpleNamespace

from agent_pm.linear_orchestrator.engine import linear_tracker
from agent_pm.linear_orchestrator.flow import workflow

# Q_ISSUE, parse_handoff and read_issue (with project_id) kept only for tests reading
# issues.<name>; 第 2 阶段删除.
from agent_pm.linear_orchestrator.flow.handoff import parse_handoff as parse_handoff
from agent_pm.utils import parse_time

Q_ISSUE = linear_tracker.Q_READ


def read_issue(gql, ident):
    issue = linear_tracker.LinearTracker(gql).issue(ident)
    return SimpleNamespace(**vars(issue), project_id=issue.home)


def is_user(note, humans) -> bool:
    return (note.account or "").lower() in humans


def brief(issue) -> str:
    h = issue.handoff
    return h.instructions if h else issue.description


@cache
def _build_started():
    """A build run's start comment: the workflow's build start lead, its colon optional."""
    leads = (t.start.removesuffix(":") for t in workflow.DEFAULT.values() if t.kind == "build")
    return re.compile(rf"(?:{'|'.join(map(re.escape, leads))})\b")


def build_cutoff(issue, humans) -> str:
    started = [
        n.at
        for n in issue.notes
        if not is_user(n, humans) and _build_started().match(n.body.strip())
    ]
    return max(started, key=parse_time) if started else issue.created_at
