"""The Adapter interface: a runtime factory from [adapters.<name>] minus `implementation`.
Its Headless (fresh per session: launch may keep state for parse) or Tui turns a prompt and a Spec into a Launch
whose env and resume carry Access.env and Spec.env (the Adapter is their only owner), and the runtime's output into
Events plus Stderr. Its InProcess checks (ConfigError refuses), then runs the session on a Driver thread, stopped at
its next yield, changing no global state (cwd, environ, signals): it applies spec.access.cwd, not its env.
select_session is the Driver's selector (registry); unset, it refuses. Starts no process."""

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping

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
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
)

TURN_END = "turn_end"
BLOCKED = "blocked"
ACTIVITY = "activity"
RESUMABLE = ("yes", "no", "unknown")


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
            raise ConfigError(f"unknown key {extra[0]!r} in {type(self).__name__}'s config")
        self.config = dict(config)

    def select_session(self, native: str, resume: bool) -> tuple[str, ...]:
        if not hasattr(self, "_selector"):
            raise ConfigError("session selector is not configured by Driver")
        return self._selector(native, resume)

    def configure_selector(self, selector: Callable[[str, bool], tuple[str, ...]]) -> None:
        self._selector = selector

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

    def usage(self, output: str) -> Usage | None:
        """None only when the probe failed."""
        return None

    def session_metadata(self, info: SessionInfo) -> SessionInfo:
        """Refresh metadata; preserve identity."""
        return info

    def session_status(self, sid: str, cwd: str) -> SessionStatus:
        return SessionStatus("unknown", None, "")

    def writable_dirs(self, spec: Spec, resume: bool) -> tuple[str, ...]:
        """Own write dirs (resume: spec.sid's)."""
        return ()


class Headless(ABC):
    def __init__(self, adapter: Adapter):
        self.adapter = adapter

    @abstractmethod
    def launch(self, prompt: str, spec: Spec, resume: bool) -> Launch:
        """The command; resume continues spec.sid."""

    @abstractmethod
    def parse(self, record: dict) -> Iterable[Event | Stderr]: ...

    def events(self, lines: Iterable[str]) -> Iterator[Event | Stderr]:
        for line in lines:
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict):
                yield from self.parse(record)


class Tui(ABC):
    hooks: Mapping[str, str] = {}  # hook label → TURN_END, BLOCKED or ACTIVITY
    dialogs: tuple[str, ...] = ()

    def __init__(self, adapter: Adapter):
        self.adapter = adapter

    @abstractmethod
    def launch(self, prompt: str, spec: Spec, resume: bool, hook: tuple[str, ...]) -> Launch:
        """As Headless.launch, plus each label L of `hooks` as a hook running `hook + (L,)`, its payload on stdin."""

    @abstractmethod
    def reply(self, payload: dict) -> str: ...

    def pending(self, payload: dict) -> int:
        return 0

    def activity(self, payload: dict) -> str:
        return ""

    def blocked_reason(self, payload: dict) -> str:
        return ""

    def session_id(self, payload: dict) -> str:
        native = payload.get("session_id")
        return native if isinstance(native, str) else ""

    def limit(self, screen: str) -> LimitHit | None:
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
    def run(self, prompt: str, spec: Spec, resume: bool) -> Iterable[Event | Stderr]:
        """The session's events; resume continues spec.sid."""

    def check(self, prompt: str, spec: Spec, resume: bool) -> None:
        pass
