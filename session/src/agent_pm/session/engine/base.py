"""Session, the ABC of an agent session on a Host."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from agent_pm.session.engine.types import (
    ConfigError,
    Event,
    IdleTimeout,
    SessionInfo,
    SessionRow,
    SessionStatus,
    Spec,
    Stderr,
    Usage,
)
from agent_pm.session.hosts import tables
from agent_pm.session.hosts.base import Host
from agent_pm.session.hosts.types import Hosts
from agent_pm.utils import Display, Record


def _later(method: str) -> NotImplementedError:
    return NotImplementedError(
        f"Session.{method}: the session layer's own implementation comes with HostedSession (TASK-365)"
    )


class Session(ABC):
    """show, attach_command, screen: NotDisplayable unless displayable (its Host is Displayable)."""

    sid: str
    runtime: str
    mode: str
    name: str | None

    @abstractmethod
    def events(self) -> Iterator[Event | Stderr | IdleTimeout]: ...

    @abstractmethod
    def send(self, text: str) -> None: ...

    @abstractmethod
    def state(self) -> str: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def info(self) -> SessionInfo: ...

    @property
    @abstractmethod
    def reply(self) -> str | None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def session_status(self) -> SessionStatus: ...

    @abstractmethod
    def record(self) -> Record | None: ...

    @property
    @abstractmethod
    def displayable(self) -> bool: ...

    @abstractmethod
    def show(self, display: Display = None) -> str | None: ...

    @abstractmethod
    def attach_command(self) -> str: ...

    @abstractmethod
    def screen(self, lines: int | None = None) -> str: ...

    @staticmethod
    def parse_hosts(table) -> Hosts:
        """An already-read [hosts] table as Hosts(default, tables); else ConfigError."""
        try:
            return tables.parse(table)
        except ValueError as error:
            raise ConfigError(str(error)) from None

    @classmethod
    def start(
        cls,
        runtime: str,
        mode: str,
        prompt: str,
        spec: Spec,
        *,
        resume: str | None = None,
        events: str | None = None,
        idle_timeout: float | None = None,
        display: Display = None,
        host: str | None = None,
    ) -> Session:
        raise _later("start")

    @classmethod
    def attach(
        cls, name: str, *, host: str | None = None, idle_timeout: float | None = None
    ) -> Session:
        raise _later("attach")

    @classmethod
    def list(cls, match: str | None = None, *, display: Display = None) -> list[SessionRow]:
        raise _later("list")

    @classmethod
    def usage(cls, runtime: str) -> Usage | None:
        raise _later("usage")

    @classmethod
    def layout(cls, display: Display = None) -> str | None:
        raise _later("layout")

    @classmethod
    def writable_dirs(cls, runtime: str, spec: Spec, resume: str | None = None) -> tuple[str, ...]:
        raise _later("writable_dirs")

    @classmethod
    def display(cls, value: Display = None, *, host: str | None = None) -> Display:
        raise _later("display")

    @classmethod
    def host(cls, name: str | None = None) -> Host:
        raise _later("host")
