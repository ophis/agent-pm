"""Delegate, the ABC of one role's run and its view."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence

from agent_pm.delegate.composer import RunConfig
from agent_pm.delegate.engine.types import Event, Result
from agent_pm.delegate.event.outcome import Outcome
from agent_pm.session import Usage, api
from agent_pm.utils.record import Record, Status


class Delegate(ABC):
    """An implementation's constructor: (role, Spec, *, optional dependencies and placement), refusing with
    ConfigError, writing nothing. run, detach run it; attach(record or workdir) views a started one. show,
    attach_command: displayable sessions only, else NotDisplayable."""

    @classmethod
    @abstractmethod
    def attach(cls, record_or_workdir: Record | str) -> Delegate: ...

    @classmethod
    @abstractmethod
    def usage(cls, role: str) -> Usage | None: ...

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

    @abstractmethod
    def detach(self, *, outer: Mapping | None = None) -> Record: ...

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
    def attach_command(self) -> str: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def session_info(self) -> api.SessionStatus: ...

    @abstractmethod
    def resume(self) -> Record: ...
