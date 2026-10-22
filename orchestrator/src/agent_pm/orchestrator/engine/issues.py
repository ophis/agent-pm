"""The Handoff's reader-side helpers: brief, is_user and build_cutoff."""

import re
from functools import cache

from agent_pm.orchestrator.engine.flow import workflow
from agent_pm.utils import parse_time


def is_user(note) -> bool:
    return note.origin == "other"


def brief(issue) -> str:
    h = issue.handoff
    return h.instructions if h else issue.description


@cache
def _lead_re(lead):
    lead = lead.removesuffix(":")
    return re.compile(rf"{re.escape(lead)}(?=\W|$)" if lead else r"(?!)")


def build_cutoff(issue) -> str:
    """A start consumes comments only after a successful build delivery on the tracker."""
    cutoff = issue.created_at
    notes = sorted(
        (n for n in issue.notes if not is_user(n) and n.run), key=lambda n: parse_time(n.at)
    )
    for task in workflow.DEFAULT.values():
        if task.input != "build":
            continue
        start, done = _lead_re(task.start), _lead_re(task.done)
        started = {}
        for note in notes:
            body = note.body.strip()
            if start.match(body):
                started[note.run] = note.at
            elif done.match(body) and note.run in started:
                cutoff = max((cutoff, started[note.run]), key=parse_time)
    return cutoff
