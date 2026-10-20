"""The interface the engine reads and writes issues through; errors are its adapter's own."""

from abc import ABC, abstractmethod
from collections.abc import Collection

from agent_pm.linear_orchestrator.flow.types import Finished, Snapshot, SubIssue


class Tracker(ABC):
    @abstractmethod
    def read(self, ident: str) -> Snapshot | None:
        """The state of the issue with KEY-N ident; None: no such issue."""

    @abstractmethod
    def finished(self, owners: Collection[str]) -> list[Finished]:
        """Every finished issue, a done one only when assigned to a role in owners, read over every
        page before it returns (archiving shifts the pages)."""

    @abstractmethod
    def sub_issues(self, key: str) -> tuple[SubIssue, ...]:
        """The direct sub-issues of the issue with handle key; archived ones are not listed."""

    @abstractmethod
    def archive(self, key: str) -> None:
        """Archive the issue with handle key and, as the tracker does, its sub-issues."""
