"""How the engine reads and writes issues; errors are the adapter's own."""

from abc import ABC, abstractmethod
from collections.abc import Collection

from agent_pm.linear_orchestrator.flow.types import Finished, Snapshot, SubIssue


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
