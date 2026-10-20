"""Abstract issue states, a Tracker's reads and a workflow's per-role Task."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any, Literal

UNREADABLE = "(unreadable)"
IssueRecord = dict[str, Any]  # Linear's issue keys until 第 2 阶段


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
    project_key: str
    project_name: str
    owner: str
    sources: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Move:
    at: str
    to: IssueState | None
    frm: IssueState | None


@dataclass(frozen=True)
class Comment:
    """other: a human's; run: a run's."""

    at: str
    body: str
    author: str | None
    other: bool
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
    project: IssueRecord | None
    labels: tuple[Label, ...]


@dataclass(frozen=True)
class Task:
    kind: Literal["research", "design", "build"]
    prefix: str = ""
    start: str = ""
    done: str = ""
    question: str = ""
    failed: str = ""
    failed_new: Literal["todo", "in_review"] = "in_review"
    retitle: bool = False
    approve: bool = False
    files: bool = False
    hint: str = ""
    next: str = ""
    require_instructions: bool = True


ISSUE_ID = r"[A-Z][A-Z0-9]*-\d+"


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
    labels: tuple[str, ...] = field(default=(), kw_only=True)


class Group(IntEnum):
    """A Related work issue's rank: its state's place in the section's order."""

    DONE = 0
    IN_REVIEW = 1
    IN_PROGRESS = 2
    TODO = 3
    BACKLOG = 4
    CANCELED = 5


@dataclass(frozen=True)
class Found:
    """A first-claim search's issue candidate; last_comment only for a Canceled one."""

    identifier: str
    title: str
    state: str
    group: Group
    urls: tuple[str, ...]
    description: str
    last_comment: str
