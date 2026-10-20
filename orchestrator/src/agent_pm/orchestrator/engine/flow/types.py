"""Abstract issue states, a Tracker's reads and a workflow's per-role Stage."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any, Literal

UNREADABLE = "(unreadable)"
IssueRecord = dict[str, Any]  # opaque until 第 2 阶段
# self: the engine; other: user or manager
Origin = Literal["self", "other"]


class IssueState(StrEnum):
    READY = "ready"
    RUNNING = "running"
    REVIEW = "review"
    APPROVED = "approved"
    DONE = "done"
    CANCELED = "canceled"


@dataclass(frozen=True)
class Snapshot:
    """state None: outside the machine; since: when it finished, else its latest move into its
    tracker state; None: unknown."""

    state: IssueState | None
    since: datetime | None


@dataclass(frozen=True)
class SubIssue:
    """key: the handle sub_issues and archive take; ident: the KEY-N read takes."""

    key: str
    ident: str
    finished: bool


@dataclass(frozen=True)
class Finished:
    """key, ident: as SubIssue's; since: None when unknown."""

    key: str
    ident: str
    since: datetime | None
    children: tuple[SubIssue, ...]


@dataclass(frozen=True)
class Ref:
    key: str
    ident: str


@dataclass(frozen=True)
class Approved:
    """Times: tracker text, kept verbatim; sources omit run records."""

    key: str
    ident: str
    url: str
    title: str
    priority: int
    created: str
    home_key: str
    home_name: str
    owner: str
    sources: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Move:
    at: str
    to: IssueState | None
    frm: IssueState | None


@dataclass(frozen=True)
class Comment:
    """run: a run's."""

    at: str
    body: str
    author: str
    origin: Origin
    run: bool


@dataclass(frozen=True)
class Detail:
    state: IssueState | None
    moves: tuple[Move, ...]
    comments: tuple[Comment, ...]
    related: frozenset[str]


@dataclass(frozen=True)
class Label:
    key: str
    name: str
    group: str | None


@dataclass(frozen=True)
class Recheck:
    ready: bool
    assignee: IssueRecord | None
    home: IssueRecord | None
    labels: tuple[Label, ...]


@dataclass(frozen=True)
class Stage:
    """A workflow stage (workflow.toml's header says each field)."""

    role: str
    input: Literal["research", "design", "build"]
    next: str
    require_instructions: bool
    title_prefix: str
    retitle: bool
    start: str
    done: str
    question: str
    failed: str
    first_failure: Literal["ready", "review"]
    approve: bool
    files: bool
    hint: str


ISSUE_ID = r"[A-Z][A-Z0-9]*-\d+"


@dataclass(frozen=True)
class Note:
    body: str
    at: str
    account: str | None
    name: str | None
    origin: Origin


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
    home: str | None
    notes: tuple[Note, ...]
    links: tuple[Link, ...]
    linked: tuple[Linked, ...]

    handoff: Handoff | None = field(kw_only=True)
    labels: tuple[str, ...] = field(default=(), kw_only=True)


class Group(IntEnum):
    """Related work's rank: its state's place in the section."""

    DONE = 0
    IN_REVIEW = 1
    IN_PROGRESS = 2
    TODO = 3
    BACKLOG = 4
    CANCELED = 5


@dataclass(frozen=True)
class Found:
    """A first-claim search's candidate; last_comment only when Canceled."""

    identifier: str
    title: str
    state: str
    group: Group
    urls: tuple[str, ...]
    description: str
    last_comment: str
