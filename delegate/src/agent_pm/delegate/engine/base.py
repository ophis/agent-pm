"""Delegate, the ABC of one role's run and its view."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence

from agent_pm.delegate.composer import RunConfig
from agent_pm.delegate.engine.types import Begun, Finished, Progress, Result
from agent_pm.delegate.event.outcome import Outcome
from agent_pm.session import Host, Session, Usage, api
from agent_pm.utils.record import Display, Record, Status


class Delegate(ABC):
    """Implementations take this constructor (refusal: ConfigError, nothing written). run, detach run it;
    attach(record or workdir) views a started one. show, attach_command: displayable sessions only, else
    NotDisplayable."""

    @abstractmethod
    def __init__(
        self,
        role: str,
        task: str | None,
        input: str,
        workdir: str,
        *,
        runtime: str | None = None,
        mode: str = "headless",
        driver=None,
        sid: str | None = None,
        name: str | None = None,
        repo: str | None = None,
        cwd: str | None = None,
        team=None,
        out: str | None = None,
        events: str | None = None,
        callback: Sequence[str] | None = None,
        display: Display = None,
        host: str | None = None,
        harness_host: str | Host | None = None,
        session_cls: type[Session] | None = None,
        composer=None,
    ) -> None: ...

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
    def run(
        self, on_event: Callable[[Begun | Progress | Finished], None] | None = None
    ) -> Result: ...

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
