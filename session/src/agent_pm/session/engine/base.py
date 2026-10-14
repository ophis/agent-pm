"""Session, the ABC of an agent session on a Host. Its class methods on Session itself are
HostedSession's (loaded when called); a subclass's are its own.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from agent_pm.session.engine.types import (
    Hosts,
    IdleTimeout,
    Liveness,
    Mode,
    ProcessOptions,
    SessionEvent,
    SessionInfo,
    SessionRow,
    SessionStatus,
    Spec,
    Stderr,
    Usage,
)
from agent_pm.session.hosts.base import Host
from agent_pm.utils import Display, Record, load_class

HOSTED = "agent_pm.session.engine.hosted:HostedSession"


class Session(ABC):
    """show, attach_command, screen: NotDisplayable unless displayable (its Host is Displayable)."""

    sid: str
    runtime: str
    mode: Mode
    name: str | None

    @abstractmethod
    def events(self) -> Iterator[SessionEvent | Stderr | IdleTimeout]: ...

    @abstractmethod
    def send(self, text: str) -> None: ...

    @abstractmethod
    def state(self) -> Liveness: ...

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
    def show(self, *, display: Display = None) -> str | None: ...

    @abstractmethod
    def attach_command(self) -> str: ...

    @abstractmethod
    def screen(self, lines: int | None = None) -> str: ...

    @classmethod
    def _hosted(cls, method: str) -> Callable[..., Any]:
        if cls is not Session:
            raise NotImplementedError(f"{cls.__name__}.{method}: not implemented")
        return getattr(load_class(HOSTED, Session), method)

    @staticmethod
    def validate_policy(*, flags, env) -> None:
        ProcessOptions(flags=flags, env=env)

    @classmethod
    def configure(cls, *, adapters: Mapping, hosts: Hosts) -> None:
        cls._hosted("configure")(adapters=adapters, hosts=hosts)

    @classmethod
    def start(
        cls,
        spec: Spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Session:
        return cls._hosted("start")(spec, display=display, host=host, idle_timeout=idle_timeout)

    @classmethod
    def attach(
        cls, name: str, *, host: str | None = None, idle_timeout: float | None = None
    ) -> Session:
        return cls._hosted("attach")(name, host=host, idle_timeout=idle_timeout)

    @classmethod
    def inspect(cls, spec: Spec) -> SessionStatus:
        """Historical status using spec.workdir, runtime and sid, without starting or attaching.
        A missing record raises DriverError.
        """
        return cls._hosted("inspect")(spec)

    @classmethod
    def list(cls, match: str | None = None, *, display: Display = None) -> list[SessionRow]:
        return cls._hosted("list")(match, display=display)

    @classmethod
    def usage(cls, runtime: str) -> Usage | None:
        return cls._hosted("usage")(runtime)

    @classmethod
    def layout(cls, *, display: Display = None) -> str | None:
        return cls._hosted("layout")(display=display)

    @classmethod
    def writable_dirs(cls, spec: Spec) -> tuple[str, ...]:
        return cls._hosted("writable_dirs")(spec)

    @classmethod
    def display(cls, value: Display = None, *, host: str | None = None) -> Display:
        return cls._hosted("display")(value, host=host)

    @classmethod
    def host(cls, name: str | None = None) -> Host:
        return cls._hosted("host")(name)
