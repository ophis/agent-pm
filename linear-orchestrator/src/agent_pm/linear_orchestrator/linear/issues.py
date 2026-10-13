"""Linear issues as the Linear orchestrator reads them (read_issue) and the one Handoff grammar:
handoff, Handoff.parse and head.
"""

import re

from agent_pm.linear_orchestrator.engine.types import IssueError
from agent_pm.linear_orchestrator.linear import linear, sessions
from agent_pm.linear_orchestrator.linear.types import (
    COMMENTS,
    HANDOFF,
    SECTIONS,
    Issue,
    Link,
    Linked,
    Note,
    description_lines,
    section_starts,
)

Q_ISSUE = """query($i: String!) { issue(id: $i) { id identifier url title description createdAt
  project { id }
  comments(first: 250) { nodes { body createdAt user { email name isMe } } }
  attachments(first: 50) { nodes { url } }
  relations(first: 50) { nodes { relatedIssue { identifier title state { name } } } }
  inverseRelations(first: 50) { nodes { issue { identifier title state { name } } } } } }"""
BUILD_STARTED = re.compile(r"Build started\b")


def read_issue(gql, ident) -> Issue:
    node = gql(Q_ISSUE, i=ident)["issue"]
    if node is None:
        raise IssueError.issue_not_found(ident)
    if node["identifier"] != ident:
        raise IssueError.linear_returned_for(str(node["identifier"])[:40], ident)
    notes = sorted(
        (
            Note(
                c["body"],
                c["createdAt"],
                (c["user"] or {}).get("email"),
                (c["user"] or {}).get("name"),
            )
            for c in node["comments"]["nodes"]
            if not sessions.is_comment(c)
        ),
        key=lambda n: linear.parse_time(n.at),
    )
    linked = {}
    for r in [x["relatedIssue"] for x in node["relations"]["nodes"]] + [
        x["issue"] for x in node["inverseRelations"]["nodes"]
    ]:
        linked.setdefault(r["identifier"], Linked(r["identifier"], r["title"], r["state"]["name"]))
    return Issue(
        id=node["id"],
        identifier=node["identifier"],
        url=node["url"],
        title=node["title"],
        description=node["description"] or "",
        created_at=node["createdAt"],
        project_id=(node["project"] or {}).get("id"),
        notes=tuple(notes),
        links=tuple(Link(a["url"]) for a in node["attachments"]["nodes"]),
        linked=tuple(linked.values()),
    )


def head(description) -> str:
    text = description or ""
    lines = description_lines(text)
    if not HANDOFF.fullmatch(lines[0]):
        m = COMMENTS.search(text)
        return text[: m.start()] if m else text
    i = section_starts(lines).get(SECTIONS[2])
    return text if i is None else text[: list(re.finditer(r"\r?\n", text))[i - 1].end()]


def handoff(identifier, url, *, sources, instructions, comments) -> str:
    """sources: (title, url) pairs; instructions and comments: (name, at, body) triples."""
    parts = [f"Handoff from {identifier}: {url}"]
    source, said, quoted = SECTIONS
    if sources:
        parts.append(
            f"{source}\n"
            + "\n".join(f"- {linear.one_line(t)}: {linear.one_line(u)}" for t, u in sources)
        )
    if instructions:
        parts.append(
            f"{said}\n" + "\n\n".join(f"{n}, {at}:\n{body}" for n, at, body in instructions)
        )
    if comments:
        # Quoted so agent-written text cannot pose as a section of this description.
        parts.append(
            f"{quoted}\n"
            + "\n".join(
                f"- {n}, {at}:\n" + "\n".join(f"  > {line}" for line in body.splitlines())
                for n, at, body in comments
            )
        )
    return "\n\n".join(parts)


def is_user(note, humans) -> bool:
    return (note.email or "").lower() in humans


def brief(issue) -> str:
    h = issue.handoff
    return h.instructions if h else issue.description


def build_cutoff(issue, humans) -> str:
    started = [
        n.at for n in issue.notes if not is_user(n, humans) and BUILD_STARTED.match(n.body.strip())
    ]
    return max(started, key=linear.parse_time) if started else issue.created_at
