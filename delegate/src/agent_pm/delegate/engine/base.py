"""Delegate, the ABC of one role's run and its view."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence

from agent_pm.delegate.composer import RunConfig
from agent_pm.delegate.engine.types import Event, Outer, Result
from agent_pm.delegate.event.outcome import Outcome
from agent_pm.session import SessionStatus, Usage
from agent_pm.utils import Display, Record, Status


class Delegate(ABC):
    """One role run and its durable view; implementations own construction and placement. Callers
    know only the run's workdir W; checkouts and clean are its layout's only outside view. An
    implementation touches only the W entries it creates, leaving every other one alone.
    """

    @classmethod
    @abstractmethod
    def attach(cls, record_or_workdir: Record | str, *, sid: str | None = None) -> Delegate:
        """The run's view; ConfigError when `sid` is given and the run is another session's."""

    @classmethod
    @abstractmethod
    def usage(cls, role: str, *, overlays: Sequence[Mapping] = ()) -> Usage | None: ...

    @classmethod
    @abstractmethod
    def roles(cls) -> dict[str, dict[str, str]]: ...

    @classmethod
    @abstractmethod
    def resolve(
        cls, role: str, task: str | None = None, *, overlays: Sequence[Mapping] = ()
    ) -> RunConfig: ...

    @classmethod
    @abstractmethod
    def trusted_dirs(cls) -> frozenset[str]: ...

    @classmethod
    @abstractmethod
    def checkouts(cls, workdir: str) -> tuple[str, ...] | None:
        """The repo checkouts W's agent files hold: directories whose last two components are the
        repo's owner and `<name>-<slug>` (utils.checkout), real paths reached through no symlink;
        () none; None: W has no agent files. Reads only.
        """

    @classmethod
    @abstractmethod
    def clean(cls, workdir: str, *, dry: bool = False) -> None:
        """Deletes W's agent files, keeping its run records; idempotent. ConfigError, nothing
        deleted, while the run is live or its agent files are not a real dir in place; dry: the
        checks only.
        """

    @classmethod
    @abstractmethod
    def check_tui(cls, *, display: Display = None, events: str | None = None) -> None:
        """ConfigError unless a tui run could show at `display` and write `events`: a value its
        agent's Host refuses or cannot place a TUI by, or an events file its harness's Host cannot
        write. Creates nothing.
        """

    @abstractmethod
    def detach(self, *, outer: Outer | None = None) -> Record: ...

    @abstractmethod
    def run(self, on_event: Callable[[Event], None] | None = None) -> Result: ...

    @abstractmethod
    def alive(self) -> bool: ...

    @abstractmethod
    def status(self) -> Status: ...

    @abstractmethod
    def outcome(self) -> Outcome | None: ...

    @abstractmethod
    def show(self) -> str | None: ...

    @abstractmethod
    def attach_command(self) -> str | None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def session_info(self) -> SessionStatus: ...

    @abstractmethod
    def resume(self, *, mode: str | None = None) -> Record: ...
