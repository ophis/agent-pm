"""The Adapter interface: a client factory from [adapters.<name>] minus `implementation`;
headless(), tui() and in_process() each give a fresh mode object, None if unsupported. Each mode
reads what to run from the Spec alone. commit() persists a validated launch before starting;
failed starts keep diagnostics. Headless launch(spec) (it may keep state for events) and Tui
launch(spec, hook) build a Launch whose env and resume carry Access.env and Spec.env (the Adapter is
their only owner), and turn the client's output into SessionEvents, Stderr and ClientBound
(JsonHeadless: parse(record) per JSON line). InProcess check(spec) refuses with ConfigError;
run(spec) runs the session on a thread of the caller's process, stopped at its next yield, changing
no global state (cwd, environ, signals): it applies spec.access.cwd, not its env. Starts no process.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType
from typing import ClassVar

from agent_pm.session.adapters.common import select
from agent_pm.session.engine.types import (
    Activity,
    Blocked,
    ClientBound,
    ConfigError,
    HookCallback,
    HookKind,
    Key,
    Launch,
    LimitHit,
    Paste,
    Resumable,
    SessionEvent,
    SessionInfo,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    TurnFailed,
    Usage,
)
from agent_pm.utils import json_records, thaw


class Adapter:
    selection: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
    forbidden_selection: tuple[str, ...] = ()
    valued_flags: tuple[str, ...] = ()
    selection_uuid: bool = False
    keys: frozenset[str] = frozenset()
    client_note: str = ""
    session_env: ClassVar[str] = ""
    identity_env: ClassVar[tuple[str, ...]] = ()
    child_defaults: ClassVar[Mapping[str, str]] = MappingProxyType({})

    def __init__(self, config: Mapping):
        if extra := sorted(set(config) - self.keys):
            key = extra[0]
            adapter = type(self).__name__
            raise ConfigError(f"unknown key {key!r} in {adapter}'s config")
        self.config = thaw(config)

    def select_session(self, native: str, *, resume: bool) -> tuple[str, ...]:
        return select(native, self.selection, uuid=self.selection_uuid, resume=resume)

    def headless(self) -> "Headless | None":
        return None

    def tui(self) -> "Tui | None":
        return None

    def in_process(self) -> "InProcess | None":
        return None

    def usage_probe(self) -> Launch | None:
        return None

    def usage(self, output: str) -> Usage | None:
        """None only when the probe failed."""
        return None

    def session_metadata(self, info: SessionInfo) -> SessionInfo:
        return info

    def session_status(self, info: SessionInfo) -> SessionStatus:
        return SessionStatus(Resumable.UNKNOWN, None, "")

    def writable_dirs(self, spec: Spec) -> tuple[str, ...]:
        """Own write dirs (spec.resume: spec.sid's)."""
        return ()


class Headless(ABC):
    def commit(self) -> None:
        return None

    @abstractmethod
    def launch(self, spec: Spec) -> Launch: ...

    @abstractmethod
    def events(self, lines: Iterable[str]) -> Iterator[SessionEvent | Stderr | ClientBound]: ...


class JsonHeadless(Headless):
    @abstractmethod
    def parse(self, record: dict) -> Iterable[SessionEvent | Stderr | ClientBound]: ...

    def events(self, lines: Iterable[str]) -> Iterator[SessionEvent | Stderr | ClientBound]:
        for record in json_records(lines):
            yield from self.parse(record)


class Tui(ABC):
    hooks: Mapping[str, HookKind] = MappingProxyType({})  # hook label → its kind
    dialogs: tuple[str, ...] = ()
    client = ""  # last ClientBound's id

    @abstractmethod
    def launch(self, spec: Spec, hook: tuple[str, ...], *, source_roots: tuple[str, ...]) -> Launch:
        """As Headless.launch, plus each label L of `hooks` as a hook running `hook + (L,)`, its
        payload on stdin. source_roots locates the loaded packages for isolated scripts.
        """

    def commit(self) -> None:
        return None

    def hook_callback(self) -> HookCallback | None:
        return None

    def restore(self, spec: Spec, state: Mapping) -> None:
        """On attach, before replay; state: hook_callback()'s state."""
        return None

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

    def observe(self) -> list[SessionEvent]:  # its client's own record, since the last call
        return []

    def replay(self, records: Iterable[tuple[str, dict]]) -> list[SessionEvent | ClientBound]:
        """On attach: observe(), then the hook log `records`' events."""
        found: list[SessionEvent | ClientBound] = [*self.observe()]
        return found + [
            event for label, payload in records for event in self.hook_events(label, payload)
        ]

    def failure(self, payload: dict) -> list[TurnFailed | LimitHit]:
        return [TurnFailed("unknown")]

    def shows(self, dialog: str, screen: str) -> bool:
        return " ".join(dialog.split()) in " ".join(screen.split())

    def limit(self, screen: str) -> LimitHit | None:
        """The client's usage-limit message on the visible terminal `screen`; None: none shown."""
        return None

    def submit(self, text: str) -> tuple[Paste | Key, ...]:
        return Paste(text), Key("Enter")

    def hook_events(self, label: str, payload: dict) -> list[SessionEvent | ClientBound]:
        found: list[SessionEvent | ClientBound] = []
        client = self.session_id(payload)
        if client and client != self.client:
            self.client = client
            found.append(ClientBound(client))
        kind = self.hooks.get(label)
        if kind == HookKind.TURN_END:
            found.append(TurnEnd(self.reply(payload), self.pending(payload)))
        elif kind == HookKind.TURN_FAILED:
            found.extend(self.failure(payload))
        elif kind == HookKind.BLOCKED:
            found.append(Blocked(self.blocked_reason(payload)))
        elif kind == HookKind.ACTIVITY:
            found.append(Activity(self.activity(payload)))
        return found


class InProcess(ABC):
    @abstractmethod
    def run(self, spec: Spec) -> Iterable[SessionEvent | Stderr | ClientBound]: ...

    def check(self, spec: Spec) -> None:
        return None
