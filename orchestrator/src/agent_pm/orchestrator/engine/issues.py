"""The Handoff's reader-side helpers: brief, is_user and build_cutoff."""

import re
from functools import cache

from agent_pm.orchestrator.engine.flow import workflow
from agent_pm.utils import parse_time


def is_user(note, humans) -> bool:
    return (note.account or "").lower() in humans


def brief(issue) -> str:
    h = issue.handoff
    return h.instructions if h else issue.description


@cache
def _build_started():
    """A build run's start comment: the workflow's build start lead, its colon optional."""
    leads = (t.start.removesuffix(":") for t in workflow.DEFAULT.values() if t.input == "build")
    return re.compile(rf"(?:{'|'.join(map(re.escape, leads))})\b")


def build_cutoff(issue, humans) -> str:
    started = [
        n.at
        for n in issue.notes
        if not is_user(n, humans) and _build_started().match(n.body.strip())
    ]
    return max(started, key=parse_time) if started else issue.created_at
