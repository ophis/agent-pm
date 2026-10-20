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
    def comment(self, key: str, body: str, ref: tuple[str, ...] | None = None) -> None: ...

    @abstractmethod
    def move(self, key: str, to: IssueState, *, frm: IssueState | None = None) -> str | None:
        """Without frm, from any state; None: moved, else its state id."""

    @abstractmethod
    def claim(self, key: str) -> None:
        """To RUNNING, its state unread: the caller rechecked it."""

    @abstractmethod
    def comment_and_move(
        self, key: str, body: str, *, to: IssueState, frm: IssueState, notify: bool
    ) -> str | None:
        """notify: subscribe others first."""

    @abstractmethod
    def notify(self, key: str, people: Collection[str]) -> str:
        """Notes for failures."""

    @abstractmethod
    def links(self, key: str) -> set[str]: ...

    @abstractmethod
    def link(self, key: str, url: str, title: str) -> None: ...

    @abstractmethod
    def retitle(self, key: str, title: str) -> None: ...

    @abstractmethod
    def on_board(self, ident: str) -> str | None:
        """Its key if on this board."""

    @abstractmethod
    def unarchive(self, key: str) -> None: ...

    @abstractmethod
    def find_comment(self, key: str, ref: tuple[str, ...]) -> str | None: ...

    @abstractmethod
    def put_comment(self, key: str, body: str, ref: tuple[str, ...]) -> bool:
        """False: refused."""

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
