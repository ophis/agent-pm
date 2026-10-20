"""Issue reads and writes; the adapter's errors."""

from abc import ABC, abstractmethod
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Literal

from agent_pm.orchestrator.engine.flow.types import (
    Approved,
    ArchivePlan,
    ArchiveRefused,
    Detail,
    Finished,
    Found,
    Issue,
    IssueRecord,
    IssueState,
    Recheck,
    Ref,
    Snapshot,
)

Listed = Literal[IssueState.READY, IssueState.RUNNING]


class Tracker(ABC):
    @abstractmethod
    def read(self, ident: str) -> Snapshot | None:
        """None: no such issue."""

    @abstractmethod
    def issue(self, ident: str) -> Issue:
        """Raises the adapter's error: none, or another's."""

    @abstractmethod
    def search(self, words: Sequence[str], idents: Sequence[str]) -> tuple[Found, ...]:
        """Archived too; idents: this board's."""

    @abstractmethod
    def description(self, ident: str) -> str | None: ...

    @abstractmethod
    def finished(self, owners: Collection[str]) -> list[Finished]:
        """Owners' done, all canceled; every page read first: archiving shifts them."""

    @abstractmethod
    def archive_plan(self, key: str) -> ArchivePlan:
        """A gone issue or an unreadable page raises the adapter's error."""

    @abstractmethod
    def _write_archive(self, key: str) -> None:
        """With its sub-issues; failure: the adapter's error."""

    def archive(self, key: str, plan: ArchivePlan | None = None) -> bool:
        """Given plan (just read): only writes. False: was archived."""
        plan = plan or self.archive_plan(key)
        if plan.outcome == "refused":
            raise ArchiveRefused(plan)
        if plan.outcome == "unchanged":
            return False
        self._write_archive(plan.key)
        return True

    @abstractmethod
    def approved(self, owners: Collection[str]) -> list[Approved]:
        """Filed in a home."""

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
        home: str,
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
        """No frm: from any state; None: moved, else its state, opaque."""

    @abstractmethod
    def claim(self, key: str) -> None:
        """To RUNNING, unread: the caller rechecked it."""

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
