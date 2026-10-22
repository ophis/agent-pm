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
    return re.compile(rf"{re.escape(lead)}(?=\W|$)" if lead else "")


def build_cutoff(issue) -> str:
    """A start consumes comments only after a successful build delivery on the tracker."""
    cutoff = issue.created_at
    notes = sorted((n for n in issue.notes if not is_user(n)), key=lambda n: parse_time(n.at))
    for task in workflow.DEFAULT.values():
        if task.input != "build":
            continue
        start = _lead_re(task.start)
        started = {}
        for note in notes:
            ref = note.run
            if ref is None:
                continue
            if ref.step == "start" and start.match(note.body.strip()):
                started[ref.sid] = note.at
            elif ref.step == "comment" and ref.status == "done" and ref.sid in started:
                cutoff = max((cutoff, started[ref.sid]), key=parse_time)
    return cutoff
