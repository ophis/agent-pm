"""Host, where processes live, keeping each one's Record and Display; Displayable, a Host that shows
them.
"""

from __future__ import annotations

import re
import signal
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence

from agent_pm.session.engine.types import ConfigError, Hosts, Key, Paste
from agent_pm.utils import CLASS_PATH, Display, Record, thaw

NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
SIGNALS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)  # detach blocks them until argv unblocks


class HostProcess(ABC):
    """close: ends it and forgets it, absent is fine; kill: its process group; wait_exit: until that
    group ends too.
    """

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
        self.settings = thaw(settings or {})

    @classmethod
    def parse_hosts(cls, table) -> Hosts:
        """The [hosts] parser's only home: an already-read [hosts] table as Hosts(default, tables),
        else ConfigError. An implementation is checked only for the module.path:Class form, never
        imported.
        """
        if not isinstance(table, Mapping):
            raise ConfigError("[hosts]: missing")
        tables = {}
        for key, value in table.items():
            if key == "default":
                continue
            if not isinstance(key, str):
                raise ConfigError(f"[hosts] {key!r}: want a string key")
            if not isinstance(value, Mapping):
                raise ConfigError(f"[hosts] {key}: want default or a [hosts.<name>] table")
            if not NAME.match(key):
                raise ConfigError(f"[hosts.{key}]: want a name matching [a-z][a-z0-9_]*")
            implementation = value.get("implementation")
            if not isinstance(implementation, str) or not CLASS_PATH.fullmatch(implementation):
                raise ConfigError(f"[hosts.{key}].implementation: want module.path:Class")
            tables[key] = value
        default = table.get("default")
        if not isinstance(default, str) or default not in tables:
            raise ConfigError(f"[hosts] default: want one of {', '.join(sorted(tables))}")
        return Hosts(default, tables)

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
    """show: None once shown, else an attach command; layout: display completed once the pane it
    goes beside is found.
    """

    @abstractmethod
    def show(self, name: str, *, display: Display = None) -> str | None: ...

    @abstractmethod
    def attach_command(self, name: str) -> str: ...

    @abstractmethod
    def screen(self, name: str, lines: int | None = None) -> str: ...

    @abstractmethod
    def layout(self, *, display: Display = None) -> str | None: ...

    @abstractmethod
    def tile(self, window: str) -> None: ...
