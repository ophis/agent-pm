"""A Linear issue read into frozen dataclasses: its notes, links, linked issues and Handoff description."""
import os
import re
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline  # noqa: E402
import sessions  # noqa: E402

Q_ISSUE = """query($i: String!) { issue(id: $i) { id identifier url title description createdAt
  state { id } project { id }
  comments(first: 250) { nodes { body createdAt user { email name isMe } } }
  attachments(first: 50) { nodes { title url } }
  relations(first: 50) { nodes { relatedIssue { identifier title state { name } } } }
  inverseRelations(first: 50) { nodes { issue { identifier title state { name } } } } } }"""
BUILD_STARTED = re.compile(r"Build started\b")
HANDOFF = re.compile(r"Handoff from ([A-Z][A-Z0-9]*-\d+): \S+")
SECTIONS = ("## Source", "## Instructions", "## Comments")


@dataclass(frozen=True)
class Note:
    """A comment; `at` is the API string."""
    body: str
    at: str
    email: str | None
    name: str | None


@dataclass(frozen=True)
class Link:
    """An attachment."""
    title: str | None
    url: str


@dataclass(frozen=True)
class Linked:
    """A related issue."""
    identifier: str
    title: str
    state: str


@dataclass(frozen=True)
class Handoff:
    """A Handoff description's section bodies, stripped."""
    source: str
    sources: str
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
    state_id: str
    project_id: str | None
    notes: tuple[Note, ...]
    links: tuple[Link, ...]
    linked: tuple[Linked, ...]

    @property
    def handoff(self) -> Handoff | None:
        return parse_handoff(self.description)


def read_issue(gql, ident) -> Issue:
    """The issue `ident`; gql must carry the harness key (isMe tells sessions.is_comment which comments are sessions).
    LookupError if it is missing or Linear returns another identifier."""
    node = gql(Q_ISSUE, i=ident)["issue"]
    if node is None:
        raise LookupError(f"{ident}: issue not found")
    if node["identifier"] != ident:
        raise LookupError(f"Linear returned {str(node['identifier'])[:40]!r} for {ident}")
    notes = sorted((Note(c["body"], c["createdAt"], (c["user"] or {}).get("email"), (c["user"] or {}).get("name"))
                    for c in node["comments"]["nodes"] if not sessions.is_comment(c)),
                   key=lambda n: pipeline.parse_time(n.at))
    linked = {}
    for r in [x["relatedIssue"] for x in node["relations"]["nodes"]] + [x["issue"] for x in node["inverseRelations"]["nodes"]]:
        linked.setdefault(r["identifier"], Linked(r["identifier"], r["title"], r["state"]["name"]))
    return Issue(
        id=node["id"], identifier=node["identifier"], url=node["url"], title=node["title"],
        description=node["description"] or "", created_at=node["createdAt"], state_id=node["state"]["id"],
        project_id=(node["project"] or {}).get("id"), notes=tuple(notes),
        links=tuple(Link(a["title"], a["url"]) for a in node["attachments"]["nodes"]), linked=tuple(linked.values()))


def parse_handoff(description) -> Handoff | None:
    """The Handoff in a description, or None if its first line is not `Handoff from <ID>: <url>`. A section starts at
    the first line exactly its header; one missing is empty."""
    lines = re.split(r"\r?\n", description or "")
    m = HANDOFF.fullmatch(lines[0])
    if not m:
        return None
    starts = {h: lines.index(h) for h in SECTIONS if h in lines}

    def body(header):
        if header not in starts:
            return ""
        end = min((s for s in starts.values() if s > starts[header]), default=len(lines))
        return "\n".join(lines[starts[header] + 1:end]).strip()
    return Handoff(m.group(1), body(SECTIONS[0]), body(SECTIONS[1]), body(SECTIONS[2]))


def is_user(note, humans) -> bool:
    """True if the note's author is one of `humans`, the lowercased human_members emails."""
    return (note.email or "").lower() in humans


def brief(issue) -> str:
    """What the user asked for: a Handoff's instructions, else the description."""
    h = issue.handoff
    return h.instructions if h else issue.description


def build_cutoff(issue, humans) -> str:
    """`at` of the latest non-user note starting `Build started`, else the issue's creation time."""
    started = [n.at for n in issue.notes if not is_user(n, humans) and BUILD_STARTED.match(n.body.strip())]
    return max(started, key=pipeline.parse_time) if started else issue.created_at
