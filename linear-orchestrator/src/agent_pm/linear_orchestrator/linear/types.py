"""Values used while reporting agent runs to Linear."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from agent_pm.delegate import Outcome
from agent_pm.linear_orchestrator.engine.types import ISSUE_ID


@dataclass(frozen=True)
class Context:
    ident: str
    issue_id: str
    role: str
    sid: str
    resume: bool
    project: str | None
    workdir: str
    gql: Callable[..., dict]
    humans: tuple[str, ...]
    states: Mapping[str, str]
    repos: Mapping[str, str]
    team: str
    target: tuple[str, str] | None


@dataclass(frozen=True, kw_only=True)
class RunIdentity:
    ident: str
    issue_id: str
    sid: str
    resume: bool
    project: str | None
    target: tuple[str, str] | None = None


@dataclass(frozen=True, kw_only=True)
class Transition:
    to: str
    frm: str
    emails: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class SessionEnd:
    returncode: int | None = None
    ended_at: str | None = None
    outcome: Outcome | None = None
    limit: bool = False


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

    @classmethod
    def parse(cls, description) -> "Handoff | None":
        lines = description_lines(description)
        m = HANDOFF.fullmatch(lines[0])
        if not m:
            return None
        starts = section_starts(lines)

        def body(header):
            if header not in starts:
                return ""
            end = min((s for s in starts.values() if s > starts[header]), default=len(lines))
            return "\n".join(lines[starts[header] + 1 : end]).strip()

        return cls(m.group(1), body(SECTIONS[1]), body(SECTIONS[2]))


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
        return Handoff.parse(self.description)


HANDOFF = re.compile(rf"Handoff from ({ISSUE_ID}): \S+")


SECTIONS = ("## Source", "## Instructions", "## Comments")


COMMENTS = re.compile(rf"^{re.escape(SECTIONS[2])}\r?$", re.M)


def description_lines(description):
    return re.split(r"\r?\n", description or "")


def section_starts(lines):
    # Only Instructions carry unquoted user text, which may hold header lines: Source is the first
    # one (unless it comes after Instructions' first), Instructions the first after it, Comments the
    # last after Instructions.
    source, instructions, comments = SECTIONS

    def find(header, after, *, last=False):
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
