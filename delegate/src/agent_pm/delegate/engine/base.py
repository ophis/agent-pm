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
    """One role run and its durable view; implementations own construction and placement. Every
    implementation lays a run's workdir out per Layout (its agent dir and run dir): prune, target
    and write-back rely on it.
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
