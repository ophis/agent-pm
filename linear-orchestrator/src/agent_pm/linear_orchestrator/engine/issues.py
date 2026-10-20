"""Linear issues as the Linear orchestrator reads them (read_issue) and the Handoff's reader-side
helpers: brief, is_user and build_cutoff."""

import re
from functools import cache

from agent_pm.linear_orchestrator.engine import run_comments
from agent_pm.linear_orchestrator.engine.types import (
    IssueError,
)
from agent_pm.linear_orchestrator.flow import workflow
from agent_pm.linear_orchestrator.flow.handoff import parse_handoff
from agent_pm.linear_orchestrator.flow.types import (
    Issue,
    Link,
    Linked,
    Note,
)
from agent_pm.utils import parse_time

Q_ISSUE = """query($i: String!) { issue(id: $i) { id identifier url title description createdAt
  project { id }
  comments(first: 250) { nodes { body createdAt user { email name isMe } } }
  attachments(first: 50) { nodes { url } }
  labels(first: 50) { nodes { id } }
  relations(first: 50) { nodes { relatedIssue { identifier title state { name } } } }
  inverseRelations(first: 50) { nodes { issue { identifier title state { name } } } } } }"""


def read_issue(gql, ident) -> Issue:
    node = gql(Q_ISSUE, i=ident)["issue"]
    if node is None:
        raise IssueError(f"{ident}: issue not found")
    if node["identifier"] != ident:
        raise IssueError(f"Linear returned {str(node['identifier'])[:40]!r} for {ident}")
    notes = sorted(
        (
            Note(
                c["body"],
                c["createdAt"],
                (c["user"] or {}).get("email"),
                (c["user"] or {}).get("name"),
            )
            for c in node["comments"]["nodes"]
            if not run_comments.is_comment(c)
        ),
        key=lambda n: parse_time(n.at),
    )
    linked = {}
    for r in [x["relatedIssue"] for x in node["relations"]["nodes"]] + [
        x["issue"] for x in node["inverseRelations"]["nodes"]
    ]:
        linked.setdefault(r["identifier"], Linked(r["identifier"], r["title"], r["state"]["name"]))
    description = node["description"] or ""
    return Issue(
        id=node["id"],
        identifier=node["identifier"],
        url=node["url"],
        title=node["title"],
        description=description,
        handoff=parse_handoff(description),
        created_at=node["createdAt"],
        project_id=(node["project"] or {}).get("id"),
        notes=tuple(notes),
        links=tuple(Link(a["url"]) for a in node["attachments"]["nodes"]),
        linked=tuple(linked.values()),
        labels=tuple(n["id"] for n in node["labels"]["nodes"]),
    )


def is_user(note, humans) -> bool:
    return (note.email or "").lower() in humans


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
