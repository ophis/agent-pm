"""How the engine reads and writes issues; errors are the adapter's own."""

from abc import ABC, abstractmethod
from collections.abc import Collection

from agent_pm.linear_orchestrator.flow.types import Finished, IssueState, Snapshot, SubIssue


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
    def find_comment(self, key: str, ref: str) -> str | None: ...

    @abstractmethod
    def put_comment(self, key: str, body: str, ref: str) -> bool:
        """False: refused."""

    @abstractmethod
    def move(self, key: str, to: IssueState, frm: IssueState | None = None) -> str | None:
        """Only from frm if given; None: in to, else its state."""

    @abstractmethod
    def comment(self, key: str, body: str, ref: str | None = None) -> None: ...

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
