"""Abstract issue states, a Tracker's reads and a workflow's per-role Task."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal


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


@dataclass(frozen=True)
class Task:
    kind: Literal["research", "design", "build"]
    prefix: str = ""
    start: str = ""
    done: str = ""
    question: str = ""
    failed: str = ""
    failed_new: str = "in_review"
    retitle: bool = False
    approve: bool = False
    files: bool = False
    hint: str = ""
