"""Abstract issue states and a Tracker's reads."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

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
