"""Linear issues as the Linear orchestrator reads them (read_issue) and the one Handoff grammar:
handoff, parse_handoff and head."""

import re
from dataclasses import dataclass

from agent_pm.linear_orchestrator.engine.types import IssueError
from agent_pm.linear_orchestrator.linear import linear, sessions

Q_ISSUE = """query($i: String!) { issue(id: $i) { id identifier url title description createdAt
  project { id }
  comments(first: 250) { nodes { body createdAt user { email name isMe } } }
  attachments(first: 50) { nodes { url } }
  relations(first: 50) { nodes { relatedIssue { identifier title state { name } } } }
  inverseRelations(first: 50) { nodes { issue { identifier title state { name } } } } } }"""
BUILD_STARTED = re.compile(r"Build started\b")
HANDOFF = re.compile(rf"Handoff from ({linear.ISSUE_ID}): \S+")
SECTIONS = ("## Source", "## Instructions", "## Comments")
COMMENTS = re.compile(rf"^{re.escape(SECTIONS[2])}\r?$", re.M)


@dataclass(frozen=True)
class Note:
    body: str
    at: str
    email: str | None
    name: str | None


@dataclass(frozen=True)
class Link:
    url: str


@dataclass(frozen=True)
class Linked:
    identifier: str
    title: str
    state: str


@dataclass(frozen=True)
class Handoff:
    source: str
    instructions: str
    comments: str


@dataclass(frozen=True)
class Issue:
    id: str
    identifier: str
    url: str
    title: str
    description: str
    created_at: str
    project_id: str | None
    notes: tuple[Note, ...]
    links: tuple[Link, ...]
    linked: tuple[Linked, ...]

    @property
    def handoff(self) -> Handoff | None:
        return parse_handoff(self.description)


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


def _lines(description):
    return re.split(r"\r?\n", description or "")


def _starts(lines):
    # Only Instructions carry unquoted user text, which may hold header lines: Source is the
    # first one (unless it
    # comes after Instructions' first), Instructions the first after it, Comments the last
    # after Instructions.
    source, instructions, comments = SECTIONS

    def find(header, after, last=False):
        found = [i for i, line in enumerate(lines) if line == header and i > after]
        return (found[-1] if last else found[0]) if found else None

    src, ins = find(source, 0), find(instructions, 0)
    if src is not None and ins is not None and src > ins:
        src = None
    if src is not None:
        ins = find(instructions, src)
    starts = {
        source: src,
        instructions: ins,
        comments: find(
            comments, max((s for s in (src, ins) if s is not None), default=0), last=True
        ),
    }
    return {h: i for h, i in starts.items() if i is not None}


def parse_handoff(description) -> Handoff | None:
    lines = _lines(description)
    m = HANDOFF.fullmatch(lines[0])
    if not m:
        return None
    starts = _starts(lines)

    def body(header):
        if header not in starts:
            return ""
        end = min((s for s in starts.values() if s > starts[header]), default=len(lines))
        return "\n".join(lines[starts[header] + 1 : end]).strip()

    return Handoff(m.group(1), body(SECTIONS[1]), body(SECTIONS[2]))


def head(description) -> str:
    text = description or ""
    lines = _lines(text)
    if not HANDOFF.fullmatch(lines[0]):
        m = COMMENTS.search(text)
        return text[: m.start()] if m else text
    i = _starts(lines).get(SECTIONS[2])
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
