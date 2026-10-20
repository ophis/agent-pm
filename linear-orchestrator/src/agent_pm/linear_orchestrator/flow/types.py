"""The engine's issue states and the issue reads a Tracker returns."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class IssueState(StrEnum):
    """The abstract state machine's states; a tracker maps its own states to them."""

    READY = "ready"
    RUNNING = "running"
    REVIEW = "review"
    APPROVED = "approved"
    DONE = "done"
    CANCELED = "canceled"


@dataclass(frozen=True)
class Snapshot:
    """state None: outside the state machine (e.g. Backlog). since: when it finished, if done or
    canceled, else its latest move into its current tracker state; None: unknown."""

    state: IssueState | None
    since: datetime | None


@dataclass(frozen=True)
class SubIssue:
    key: str
    ident: str
    finished: bool


@dataclass(frozen=True)
class Finished:
    """key: the tracker's handle for reads and writes; since: when it finished, None: unknown."""

    key: str
    ident: str
    since: datetime | None
    children: tuple[SubIssue, ...]
