"""The Adapter interface: a runtime factory from [adapters.<name>] minus `implementation`. Its
Headless (fresh per session: launch may keep state for parse) or Tui turns a prompt and request into
a Launch whose env and resume carry Access.env and Spec.env (the Adapter is their only owner), and
the runtime's output into Events plus Stderr. Its InProcess checks (ConfigError refuses), then runs
the session on a Driver thread, stopped at its next yield, changing no global state (cwd, environ,
signals): it applies spec.access.cwd, not its env. Starts no process.
"""

import copy
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from agent_pm.session.adapters.selection import select
from agent_pm.session.engine.types import (
    Activity,
    Blocked,
    ConfigError,
    Event,
    Key,
    Launch,
    LimitHit,
    Paste,
    SessionBound,
    SessionInfo,
    SessionRequest,
    SessionStatus,
    Stderr,
    TurnEnd,
    Usage,
)
from agent_pm.session.hosts import tables
from agent_pm.session.hosts.tables import NAME
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.types import HostsError
from agent_pm.utils import json_records
from agent_pm.utils import load_class as load

TURN_END = "turn_end"
BLOCKED = "blocked"
ACTIVITY = "activity"


class Adapter(ABC):
    selection: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
    forbidden_selection: tuple[str, ...] = ()
    valued_flags: tuple[str, ...] = ()
    selection_uuid: bool = False
    keys: frozenset[str] = frozenset()
    runtime_note: str = ""
    session_env: str = ""
    identity_env: tuple[str, ...] = ()
    child_defaults: Mapping[str, str] = {}

    def __init__(self, config: Mapping):
        if extra := sorted(set(config) - self.keys):
            raise ConfigError.unknown_adapter_key(extra[0], type(self).__name__)
        self.config = dict(config)

    def select_session(self, native: str, *, resume: bool) -> tuple[str, ...]:
        return select(native, self.selection, uuid=self.selection_uuid, resume=resume)

    @abstractmethod
    def headless(self) -> "Headless | None":
        """A fresh Headless, or None."""

    @abstractmethod
    def tui(self) -> "Tui | None": ...

    def in_process(self) -> "InProcess | None":
        """A fresh InProcess, or None."""
        return None

    def usage_probe(self) -> Launch | None:
        return None

    def usage(self, _output: str, /) -> Usage | None:
        """None only when the probe failed."""
        return None

    def session_metadata(self, info: SessionInfo) -> SessionInfo:
        return info

    def session_status(self, _sid: str, _cwd: str, /) -> SessionStatus:
        return SessionStatus("unknown", None, "")

    def writable_dirs(self, _request: SessionRequest, /) -> tuple[str, ...]:
        """Own write dirs (resume: request.spec.sid's)."""
        return ()


class Headless(ABC):
    def __init__(self, adapter: Adapter):
        self.adapter = adapter

    @abstractmethod
    def launch(self, prompt: str, request: SessionRequest) -> Launch:
        """The command; request.resumed continues request.spec.sid."""

    @abstractmethod
    def parse(self, record: dict) -> Iterable[Event | Stderr]: ...

    def events(self, lines: Iterable[str]) -> Iterator[Event | Stderr]:
        for record in json_records(lines):
            yield from self.parse(record)


class Tui(ABC):
    hooks: Mapping[str, str] = {}  # hook label → TURN_END, BLOCKED or ACTIVITY
    dialogs: tuple[str, ...] = ()

    def __init__(self, adapter: Adapter):
        self.adapter = adapter

    @abstractmethod
    def launch(self, prompt: str, request: SessionRequest, hook: tuple[str, ...]) -> Launch:
        """As Headless.launch, plus each label L of `hooks` as a hook running `hook + (L,)`, its
        payload on stdin.
        """

    def hook_state(self) -> dict:
        return {}

    @abstractmethod
    def reply(self, payload: dict) -> str: ...

    def pending(self, _payload: dict, /) -> int:
        return 0

    def activity(self, _payload: dict, /) -> str:
        return ""

    def blocked_reason(self, _payload: dict, /) -> str:
        return ""

    def session_id(self, payload: dict) -> str:
        native = payload.get("session_id")
        return native if isinstance(native, str) else ""

    def limit(self, _screen: str, /) -> LimitHit | None:
        """The runtime's usage-limit message on the visible pane `screen`; None: none shown."""
        return None

    def submit(self, text: str) -> tuple[Paste | Key, ...]:
        return Paste(text), Key("Enter")

    def hook_events(self, label: str, payload: dict, bound: str = "") -> list[Event]:
        found = []
        native = self.session_id(payload)
        if native and native != bound:
            found.append(SessionBound(native))
        kind = self.hooks.get(label)
        if kind == TURN_END:
            found.append(TurnEnd(self.reply(payload), self.pending(payload)))
        elif kind == BLOCKED:
            found.append(Blocked(self.blocked_reason(payload)))
        elif kind == ACTIVITY:
            found.append(Activity(self.activity(payload)))
        return found


class InProcess(ABC):
    def __init__(self, adapter: Adapter):
        self.adapter = adapter

    @abstractmethod
    def run(self, prompt: str, request: SessionRequest) -> Iterable[Event | Stderr]:
        """The session's events; request.resumed continues request.spec.sid."""

    def check(self, _request: SessionRequest, /) -> None:
        return None


PACKAGE = "agent_pm.session"
IMPLEMENTATION = re.compile(r"adapters\.(?P<name>[^:]+):(?P<cls>[A-Za-z_][A-Za-z0-9_]*Adapter)\Z")
TUI_KEYS = ("status_line", "workers_per_column", "grid_retile")


@dataclass(frozen=True)
class TuiSettings:
    """tmux's status line, a grid's panes per column, when it re-tiles (None: tmux_host's)."""

    status_line: bool = False
    per_column: int | None = None
    retile: str | None = None


@dataclass(frozen=True)
class Settings:
    adapters: dict[str, dict]
    hosts: dict[str, dict]
    default_host: str


def settings(config: Mapping) -> dict:
    if not isinstance(config, Mapping):
        raise HostsError.session_table()
    found = tables.parse(config.get("hosts"))
    return {
        "adapters": config.get("adapters", {}),
        "hosts": found.tables,
        "default_host": found.default,
    }


class Registry:
    """Caller-owned adapter and host configuration."""

    def __init__(self, *, adapters: Mapping, hosts: Mapping, default_host: str) -> None:
        if not isinstance(adapters, Mapping) or not all(
            isinstance(k, str) and isinstance(t, Mapping) for k, t in adapters.items()
        ):
            raise ConfigError.adapter_tables()
        if not isinstance(hosts, Mapping) or "default" in hosts:
            raise ConfigError.host_tables()
        try:
            found = tables.parse({**hosts, "default": default_host})
        except ValueError as e:
            raise ConfigError(str(e)) from None
        settings = Settings(
            copy.deepcopy({k: dict(t) for k, t in adapters.items()}), found.tables, found.default
        )
        self.settings = settings

    def configured(self) -> list[str]:
        return sorted(self.settings.adapters)

    def tui(self) -> TuiSettings:
        table = self.settings.hosts.get(tmux_host.HOST, {})
        line, per, retile = (table.get(k) for k in TUI_KEYS)
        if line is not None and not isinstance(line, bool):
            raise ConfigError.status_line()
        if per is not None and (
            isinstance(per, bool) or not isinstance(per, int) or not 1 <= per <= 9999
        ):
            raise ConfigError.workers_per_column()
        if retile is not None and (not isinstance(retile, str) or retile not in tmux_host.RETILES):
            raise ConfigError.grid_retile(tmux_host.RETILES)
        return TuiSettings(bool(line), per, retile)

    def adapter(self, name: str) -> Adapter:
        if not isinstance(name, str) or not NAME.match(name):
            raise ConfigError.runtime_name(name)
        table = self.settings.adapters.get(name)
        if table is None:
            raise ConfigError.missing_adapter(name)
        config = dict(table)
        implementation = config.pop("implementation", None)
        found = IMPLEMENTATION.match(implementation) if isinstance(implementation, str) else None
        if not found or found["name"] != name:
            raise ConfigError.adapter_implementation(name)
        try:
            cls = load(f"{PACKAGE}.adapters.{name}:{found['cls']}", Adapter)
        except ValueError as e:
            raise ConfigError.adapter_load(name, e) from e
        return cls(config)
