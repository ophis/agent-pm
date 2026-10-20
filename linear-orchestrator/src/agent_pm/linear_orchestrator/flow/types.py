"""Abstract issue states and a Tracker's reads."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

UNREADABLE = "(unreadable)"
Record = dict[str, Any]  # Linear's issue keys until 第 2 阶段


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
    """key and ident as SubIssue's; since: None when unknown."""

    key: str
    ident: str
    since: datetime | None
    children: tuple[SubIssue, ...]
