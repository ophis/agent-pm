"""Abstract issue states and a Tracker's reads."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


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
    key: str
    ident: str
    finished: bool


@dataclass(frozen=True)
class Finished:
    key: str
    ident: str
    since: datetime | None
    children: tuple[SubIssue, ...]
