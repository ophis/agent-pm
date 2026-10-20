"""A local board's issue file and its parts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from agent_pm.orchestrator.engine.flow.types import IssueState


class LocalState(StrEnum):
    """A state dir's name: backlog, then IssueState's by value."""

    BACKLOG = "backlog"
    READY = IssueState.READY.value
    RUNNING = IssueState.RUNNING.value
    REVIEW = IssueState.REVIEW.value
    APPROVED = IssueState.APPROVED.value
    DONE = IssueState.DONE.value
    CANCELED = IssueState.CANCELED.value


class Source(StrEnum):
    """Who wrote a History line or comment: the engine (self) or a person (other)."""

    SELF = "self"
    OTHER = "other"


@dataclass(frozen=True)
class Entry:
    """A History line."""

    at: datetime
    state: LocalState
    source: Source


@dataclass(frozen=True)
class Note:
    """A comment; key: its hidden key, None if none."""

    at: datetime
    source: Source
    body: str
    key: str | None


@dataclass(frozen=True)
class FileLink:
    title: str
    url: str


@dataclass(frozen=True)
class IssueFile:
    """assignee: its assignee dir, None when archived; state: its located state; history sorted by
    time, equal times in file order."""

    path: str
    assignee: str | None
    state: LocalState | None
    mtime: datetime
    id: str
    title: str
    task: str | None
    client: str | None
    priority: int
    created: datetime
    blocked_by: tuple[str, ...]
    related: tuple[str, ...]
    parent: str | None
    links: tuple[FileLink, ...]
    description: str
    comments: tuple[Note, ...]
    history: tuple[Entry, ...]
