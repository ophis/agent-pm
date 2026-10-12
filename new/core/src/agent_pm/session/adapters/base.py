"""The Adapter interface: a runtime factory from [adapters.<name>] minus `implementation`.
Its Headless (a fresh one per session: launch may keep state for parse) or Tui turns a prompt and a Spec into a
Launch whose env (last) and resume carry a role run's Access.env, and the runtime's output into the frozen event
set (Event) plus Stderr. Its InProcess checks (ConfigError refuses), then runs the session on a Driver thread, stopped
at its next yield; it changes no global state (cwd, environ, signals), applying spec.access.cwd itself, not its env.
Access.dirs are granted write, Access.reads only read. Spec.flags/env are caller-supplied for every Mode. Times are epoch seconds. Starts no process."""

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from agent_pm.utils.paths import printable_abs

EFFORTS = ("low", "medium", "high", "xhigh", "max")
TURN_END = "turn_end"
BLOCKED = "blocked"
ACTIVITY = "activity"


class ConfigError(Exception):
    pass


def _strings(value) -> bool:
    return isinstance(value, tuple) and all(isinstance(s, str) for s in value)


def _clean(value) -> bool:
    return isinstance(value, str) and "\0" not in value


def env_ok(env) -> bool:
    return isinstance(env, Mapping) and all(
        isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and _clean(v)
        for k, v in env.items()
    )


def sid_error(sid) -> str | None:
    if isinstance(sid, str) and sid and sid.isprintable() and "/" not in sid:
        return None
    return f'sid must be a non-empty printable string without "/", got {sid!r:.80}'


@dataclass(frozen=True, kw_only=True)
class Access:
    dirs: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    cwd: str = ""
    project: bool = False
    reads: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        for name in ("dirs", "commands", "reads"):
            if not _strings(getattr(self, name)):
                raise ConfigError(f"{name} must be a tuple of strings")
        if not env_ok(self.env):
            raise ConfigError(
                "access env must map names ([A-Za-z_][A-Za-z0-9_]*) to strings without NUL"
            )


@dataclass(frozen=True, kw_only=True)
class Spec:
    workdir: str
    access: Access = Access()
    tier: int | None = None
    effort: str | None = None
    sid: str | None = None
    flags: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    _select: Callable[[str, bool], tuple[str, ...]] = field(
        default=lambda native, resume: (), repr=False, compare=False
    )
    name: str | None = None
    protect: tuple[str, ...] = ()  # paths no writable dir may overlap (Codex only)

    def __post_init__(self):
        if not printable_abs(self.workdir):
            raise ConfigError(
                f"workdir must be a printable absolute path, got {self.workdir!r:.80}"
            )
        if self.tier is not None and (
            isinstance(self.tier, bool) or not isinstance(self.tier, int) or not 1 <= self.tier <= 4
        ):
            raise ConfigError(f"tier must be an integer 1–4, got {self.tier!r}")
        if self.effort is not None and self.effort not in EFFORTS:
            raise ConfigError(f"effort must be one of {', '.join(EFFORTS)}, got {self.effort!r}")
        if self.access.cwd != "" and not printable_abs(self.access.cwd):
            raise ConfigError(f"cwd must be a printable absolute path, got {self.access.cwd!r:.80}")
        if self.sid is not None and (error := sid_error(self.sid)):
            raise ConfigError(error)
        if not (isinstance(self.flags, tuple) and all(map(_clean, self.flags))):
            raise ConfigError("flags must be a tuple of strings without NUL")
        if not env_ok(self.env):
            raise ConfigError("env must map variable names to strings without NUL")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        if self.name is not None and not isinstance(self.name, str):
            raise ConfigError("name must be None or a string")
        if not (isinstance(self.protect, tuple) and all(map(printable_abs, self.protect))):
            raise ConfigError("protect must be a tuple of printable absolute paths")


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    unset: tuple[str, ...] = ()
    cwd: str = ""
    transcript: str = ""
    resume: str = ""  # a human's shell command, each interpolated field shlex.quoted

    project: bool | None = None
    replay_env: bool = False

    def __post_init__(self):
        if type(self.replay_env) is not bool:
            raise ConfigError("launch: replay_env must be a bool")
        if self.project is not None and not isinstance(self.project, bool):
            raise ConfigError("launch: project must be a bool or None")
        env = self.env
        for name, ok in (
            ("argv", isinstance(self.argv, tuple) and all(map(_clean, self.argv))),
            (
                "env",
                isinstance(env, Mapping) and all(_clean(k) and _clean(v) for k, v in env.items()),
            ),
            ("unset", isinstance(self.unset, tuple) and all(map(_clean, self.unset))),
        ):
            if not ok:
                raise ConfigError(f"launch: {name} must be strings without NUL")


@dataclass(frozen=True)
class SessionInfo:
    sid: str
    native_id: str
    cwd: str
    project: bool
    transcript: str
    resume: str


@dataclass(frozen=True)
class SessionBound:
    native_id: str


@dataclass(frozen=True)
class Activity:
    detail: str = ""


@dataclass(frozen=True)
class Reply:
    text: str


@dataclass(frozen=True)
class TurnEnd:
    reply: str = ""
    pending: int = 0


@dataclass(frozen=True)
class Blocked:
    reason: str = ""


@dataclass(frozen=True)
class LimitHit:
    resets_at: float | None = None


@dataclass(frozen=True)
class Exited:
    returncode: int


Event = SessionBound | Activity | Reply | TurnEnd | Blocked | LimitHit | Exited

RESUMABLE = ("yes", "no", "unknown")


@dataclass(frozen=True)
class Window:
    used_percent: float  # may exceed 100
    resets_at: float | None  # None: not given


@dataclass(frozen=True)
class Usage:
    five_hour: Window | None  # None: not reported
    weekly: Window | None


@dataclass(frozen=True)
class SessionStatus:
    resumable: str
    last_active: float | None
    resume_command: str


@dataclass(frozen=True)
class Stderr:
    text: str


@dataclass(frozen=True)
class Paste:
    text: str


@dataclass(frozen=True)
class Key:
    name: str


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
    def tui(self) -> "Tui | None":
        """The Tui, or None."""

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
    def parse(self, record: dict) -> Iterable[Event | Stderr]:
        """One JSONL object's events."""

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
    def reply(self, payload: dict) -> str:
        """A TURN_END hook's reply."""

    def pending(self, payload: dict) -> int:
        return 0

    def activity(self, payload: dict) -> str:
        """An ACTIVITY hook's detail."""
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
