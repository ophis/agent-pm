"""Issue reads and writes; the adapter's errors."""

from abc import ABC, abstractmethod
from collections.abc import Collection
from datetime import datetime
from typing import Literal

from agent_pm.linear_orchestrator.flow.types import (
    Approved,
    Detail,
    Finished,
    IssueRecord,
    IssueState,
    Recheck,
    Ref,
    Snapshot,
    SubIssue,
)

Listed = Literal[IssueState.READY, IssueState.RUNNING]


class Tracker(ABC):
    @abstractmethod
    def read(self, ident: str) -> Snapshot | None:
        """None: no such issue."""

    @abstractmethod
    def finished(self, owners: Collection[str]) -> list[Finished]:
        """Owners' done issues and all canceled ones; all pages read first (archiving shifts them)."""

    @abstractmethod
    def sub_issues(self, key: str) -> tuple[SubIssue, ...]:
        """Direct ones, not archived."""

    @abstractmethod
    def archive(self, key: str) -> None:
        """Its sub-issues too, as the tracker does."""

    @abstractmethod
    def approved(self, owners: Collection[str]) -> list[Approved]:
        """In a project."""

    @abstractmethod
    def detail(self, key: str) -> Detail: ...

    @abstractmethod
    def created(self, once: tuple[str, ...]) -> Ref | None:
        """Archived too."""

    @abstractmethod
    def create(
        self,
        once: tuple[str, ...],
        *,
        project: str,
        owner: str,
        priority: int,
        title: str,
        description: str,
    ) -> Ref:
        """READY."""

    @abstractmethod
    def relate(self, key: str, other: str) -> None: ...

    @abstractmethod
    def comment(self, key: str, body: str) -> None: ...

    @abstractmethod
    def move(self, key: str, to: IssueState, *, frm: IssueState) -> str | None:
        """Only from frm; None: moved, else its state id."""

    @abstractmethod
    def comment_and_move(
        self, key: str, body: str, *, to: IssueState, frm: IssueState, notify: bool
    ) -> str | None:
        """notify: subscribe others first."""

    @abstractmethod
    def role(self, issue: IssueRecord) -> str: ...

    @abstractmethod
    def assigned(self, state: Listed) -> list[IssueRecord]: ...

    @abstractmethod
    def ready(self) -> list[tuple[IssueRecord, tuple[str, ...]]]: ...

    @abstractmethod
    def moved_at(self, key: str, state: Listed, *, by_people: bool = False) -> datetime | None: ...

    @abstractmethod
    def recheck(self, key: str) -> Recheck: ...
