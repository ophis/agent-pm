"""Host, where processes live, keeping each one's Record and Display; Displayable, a Host that shows
them.
"""

from __future__ import annotations

import os
import signal
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence

from agent_pm.session.engine.types import (
    ConfigError,
    HookCallback,
    Hosts,
    Key,
    KeyPress,
    Launch,
    Liveness,
    Paste,
    SessionError,
    SessionRow,
    StatusState,
    TuiSettings,
)
from agent_pm.utils import (
    CLASS_PATH,
    CONFIG_NAME,
    STATE,
    Display,
    Record,
    Status,
    append_line,
    printable_abs,
    thaw,
)

SIGNALS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
TUI_DIR = f"{STATE}/tui"


def tui_lock(name: str) -> str:
    return os.path.join(os.path.expanduser(TUI_DIR), f"{name}.lock")


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

    @property
    def identity(self) -> str:
        return self.name

    @property
    def token(self) -> str | None:
        return None

    def publish(self, record: Record) -> None:
        raise SessionError(f"session {self.name}: its host cannot store a Record after launch")

    def session_identity(self) -> tuple[str | None, str | None]:
        """Private terminal sid and generation, independent of its published Record."""
        return None, None

    def state(self) -> Liveness:
        return Liveness.LIVE if self.alive() else Liveness.ABSENT

    def returncode(self) -> int | None:
        return None if self.alive() else -signal.SIGHUP

    def activity(self) -> StatusState | None:
        return None

    def hidden(self) -> bool:
        return False

    def status(self) -> Status:
        liveness = self.state()
        if liveness == Liveness.UNKNOWN:
            state = StatusState.UNKNOWN
        elif liveness == Liveness.LIVE:
            state = self.activity() or StatusState.WORKING
        else:
            state = StatusState.DEAD
        return Status(
            name=self.name,
            alive=liveness == Liveness.LIVE,
            state=state,
            outcome=None,
            attach=None,
            hidden=self.hidden(),
        )

    def mark(self, word: StatusState, events: str | None) -> None:
        """Optional activity metadata for a terminal's status display."""
        return None

    def row(self) -> SessionRow:
        receipt = self.record()
        return SessionRow(
            self.name,
            None if receipt is None else receipt.sid,
            None if receipt is None else receipt.kind,
            StatusState.WORKING if self.alive() else StatusState.DEAD,
            self.display(),
            None,
            None,
            own=False,
        )

    @abstractmethod
    def record(self) -> Record | None: ...

    @abstractmethod
    def display(self) -> Display: ...


class Terminal(HostProcess):
    """send: into its input (Key names: the Adapter's)."""

    @abstractmethod
    def send(self, action: Paste | Key) -> None: ...

    @abstractmethod
    def press(self, keys: Sequence[KeyPress]) -> None: ...


class Host(ABC):
    """settings: [hosts.<name>] minus implementation. Launch.env is passed verbatim;
    on exit, events gets `exited <rc>` / `dead`.
    attach: a Terminal, unknown → SessionError. list: sorted display
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
            if not CONFIG_NAME.match(key):
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
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
    ) -> HostProcess: ...

    @abstractmethod
    def terminal(
        self,
        name: str,
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> Terminal:
        """metadata carries private sid/client/generation separately from the published Record."""
        ...

    @abstractmethod
    def attach(self, name: str) -> HostProcess: ...

    @abstractmethod
    def list(self, display: Display = None) -> list[str]: ...

    def rows(self, display: Display = None) -> list[SessionRow]:
        return [self.attach(name).row() for name in self.list(display)]

    def reclaim(self, token: str) -> HostProcess | None:
        """An untrusted token's live process group, else None."""
        return None

    def descendants(self, display: Display = None) -> list[str]:
        """list(display) and, transitively, those they opened."""
        return self.list(display)

    def events_path(self, events: str | None) -> str | None:
        return None if events is None else os.path.abspath(events)

    def prepare_events(self, events: str | None) -> str | None:
        if events is not None:
            try:
                append_line(events, "")
            except OSError as error:
                raise SessionError(f"events file {events}: {error.strerror}") from error
        return events

    def hook_callback(self, name: str, metadata: Mapping[str, str]) -> HookCallback | None:
        return None

    def tui_settings(self) -> TuiSettings:
        return TuiSettings()

    def validate(self, name: str, launch: Launch, *, display: Display = None) -> None:
        self.validate_display(display)
        if not launch.argv:
            raise ConfigError("no command")
        if not printable_abs(launch.cwd):
            raise ConfigError(f"cwd must be a printable absolute path, got {launch.cwd!r:.80}")

    def validate_display(self, value: Display) -> None:
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"display {value!r:.80}: want text")

    def display(self, value: Display) -> Display:
        self.validate_display(value)
        return value

    def display_help(self) -> str:
        return ""


class Displayable(ABC):
    """show: None once shown, else an attach command; command: a shell command the Host runs to
    show it instead, `{{session}}` the session name; hide: undoes show, marking it hidden until a
    show; layout: display completed once its display destination is found.
    """

    @abstractmethod
    def show(
        self, name: str, *, display: Display = None, command: str | None = None
    ) -> str | None: ...

    @abstractmethod
    def hide(self, name: str) -> None: ...

    @abstractmethod
    def attach_command(self, name: str) -> str: ...

    @abstractmethod
    def screen(self, name: str | HostProcess, lines: int | None = None) -> str: ...

    @abstractmethod
    def layout(self, *, display: Display = None) -> str | None: ...

    @abstractmethod
    def tile(self, window: str, *, wait: bool = True, if_panes_changed: bool = False) -> None: ...
