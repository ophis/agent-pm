"""Hosts own processes, Records and Displays; Displayable Hosts show them."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence

from agent_pm.session.engine.types import DriverError, Key, Paste
from agent_pm.utils import Display, Record


class NotDisplayable(DriverError):
    """`host <name> cannot display`."""


class HostProcess(ABC):
    """close: ends it and forgets it, absent is fine; kill: its process group."""

    name: str

    @abstractmethod
    def alive(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def wait_exit(self, timeout: float) -> bool: ...

    @abstractmethod
    def kill(self) -> None: ...

    @abstractmethod
    def record(self) -> Record | None: ...

    @abstractmethod
    def display(self) -> Display: ...


class Terminal(HostProcess):
    """send: into its input (Key names: the Adapter's)."""

    @abstractmethod
    def send(self, action: Paste | Key) -> None: ...


class Host(ABC):
    """settings: [hosts.<name>] minus implementation. detach/terminal: env verbatim; on exit, events
    gets `exited <rc>` / `dead`. attach: a Terminal, unknown → DriverError. list: sorted display
    matches ("" none). display: completed in the caller's process.
    """

    def __init__(self, name: str, settings: Mapping | None = None) -> None:
        self.name = name
        self.settings = dict(settings or {})

    @abstractmethod
    def detach(
        self,
        name: str,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
    ) -> HostProcess: ...

    @abstractmethod
    def terminal(
        self,
        name: str,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
    ) -> Terminal: ...

    @abstractmethod
    def attach(self, name: str) -> HostProcess: ...

    @abstractmethod
    def list(self, display: Display = None) -> list[str]: ...

    def display(self, value: Display) -> Display:
        return value

    def display_help(self) -> str:
        return ""


class Displayable(ABC):
    """show: None once shown, else an attach command; layout: api.layout's, split folded into
    display.
    """

    @abstractmethod
    def show(self, name: str, display: Display = None) -> str | None: ...

    @abstractmethod
    def attach_command(self, name: str) -> str: ...

    @abstractmethod
    def screen(self, name: str, lines: int | None = None) -> str: ...

    @abstractmethod
    def layout(self, display: Display = None) -> str | None: ...

    @abstractmethod
    def tile(self, window: str) -> None: ...
