"""Values used while reporting agent runs to Linear."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from agent_pm.delegate import Outcome


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

    handoff: Handoff | None = field(kw_only=True)
