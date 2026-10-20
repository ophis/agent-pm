"""How the engine reads and writes issues; errors are the adapter's own."""

from abc import ABC, abstractmethod
from collections.abc import Collection
from datetime import datetime
from typing import Literal

from agent_pm.linear_orchestrator.flow.types import (
    Finished,
    IssueRecord,
    IssueState,
    Recheck,
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
        """Owners' done issues and every canceled one, all pages read first (archiving shifts them)."""

    @abstractmethod
    def sub_issues(self, key: str) -> tuple[SubIssue, ...]:
        """Direct ones, archived ones left out."""

    @abstractmethod
    def archive(self, key: str) -> None:
        """Its sub-issues too, as the tracker does."""

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
