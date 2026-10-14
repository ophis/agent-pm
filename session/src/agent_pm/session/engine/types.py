"""The session layer's data types, used by two or more of its modules. Access.dirs are granted
write, Access.reads only read; Spec.flags/env are caller-supplied for every Mode; times are epoch
seconds.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from agent_pm.utils import (
    environment_ok,
    freeze,
    printable_abs,
    sid_error,
)

__all__ = [
    "Access",
    "Activity",
    "ActivityState",
    "Blocked",
    "ClientBound",
    "ConfigError",
    "DriverError",
    "Effort",
    "Exited",
    "HookError",
    "HookKind",
    "Hosts",
    "IdleTimeout",
    "Key",
    "Launch",
    "LimitHit",
    "Liveness",
    "Mode",
    "NotDisplayable",
    "NoRecordedSession",
    "Paste",
    "Prepared",
    "ProcessOptions",
    "Reply",
    "Resumable",
    "SessionEvent",
    "SessionInfo",
    "SessionRow",
    "SessionStatus",
    "Spec",
    "Stderr",
    "Tier",
    "TurnEnd",
    "Usage",
    "Window",
    "Word",
]


def _clean_string(value) -> bool:
    return isinstance(value, str) and "\0" not in value


class ConfigError(Exception):
    pass


class Mode(StrEnum):
    HEADLESS = "headless"
    TUI = "tui"
    IN_PROCESS = "in_process"


class Liveness(StrEnum):
    LIVE = "live"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class Resumable(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class HookKind(StrEnum):
    TURN_END = "turn_end"
    BLOCKED = "blocked"
    ACTIVITY = "activity"


class Tier(StrEnum):
    """Spec.tier's values (an int there), as the keys of an adapter's tiers table."""

    ONE = "1"
    TWO = "2"
    THREE = "3"
    FOUR = "4"


class Word(StrEnum):
    """A tui hook's mark for its pane's activity."""

    DONE = "done"
    BLOCKED = "blocked"
    WORKING = "working"


class ActivityState(StrEnum):
    """A listed session's activity: its pane's Word, dead once its pane died."""

    WORKING = "working"
    DONE = "done"
    BLOCKED = "blocked"
    DEAD = "dead"


class HookError(Exception):
    """One stderr line; the hook stops."""


def _member(enum, field: str, value):
    try:
        return enum(value)
    except ValueError:
        raise ConfigError(f"{field} must be one of {', '.join(enum)}, got {value!r}") from None


TAG = re.compile(r"[A-Za-z0-9_-]+")


def _string_tuple(value) -> bool:
    return isinstance(value, tuple) and all(isinstance(s, str) for s in value)


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
            if not _string_tuple(getattr(self, name)):
                raise ConfigError(f"{name} must be a tuple of strings")
        if not environment_ok(self.env):
            raise ConfigError("access env must map variable names to strings without NUL")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True, kw_only=True)
class ProcessOptions:
    flags: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not (isinstance(self.flags, tuple) and all(map(_clean_string, self.flags))):
            raise ConfigError("flags must be a tuple of strings without NUL")
        if not environment_ok(self.env):
            raise ConfigError("env must map variable names to strings without NUL")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True, kw_only=True)
class Spec(ProcessOptions):
    workdir: str
    access: Access = field(default_factory=Access)
    tier: int | None = None
    effort: Effort | None = None
    sid: str | None = None
    name: str | None = None
    protect: tuple[str, ...] = ()  # paths no writable dir may overlap (a sandboxing Adapter only)
    reply_tags: tuple[str, str] | None = None  # caller's (progress, outcome) tags for in_process
    runtime: str | None = None
    mode: Mode | None = None
    prompt: str = ""
    resume: bool = False  # resume session sid
    events: str | None = None

    def __post_init__(self):
        if not printable_abs(self.workdir):
            raise ConfigError(
                f"workdir must be a printable absolute path, got {self.workdir!r:.80}"
            )
        if self.tier is not None and (
            isinstance(self.tier, bool)
            or not isinstance(self.tier, int)
            or str(self.tier) not in tuple(Tier)
        ):
            raise ConfigError(f"tier must be an integer 1–4, got {self.tier!r}")
        if self.effort is not None:
            object.__setattr__(self, "effort", _member(Effort, "effort", self.effort))
        if self.access.cwd != "" and not printable_abs(self.access.cwd):
            raise ConfigError(f"cwd must be a printable absolute path, got {self.access.cwd!r:.80}")
        if self.sid is not None and (error := sid_error(self.sid)):
            raise ConfigError(error)
        super().__post_init__()
        if self.name is not None and not isinstance(self.name, str):
            raise ConfigError("name must be None or a string")
        if not (isinstance(self.protect, tuple) and all(map(printable_abs, self.protect))):
            raise ConfigError("protect must be a tuple of printable absolute paths")
        if self.reply_tags is not None and not (
            isinstance(self.reply_tags, tuple)
            and len(self.reply_tags) == 2
            and all(isinstance(t, str) and TAG.fullmatch(t) for t in self.reply_tags)
        ):
            raise ConfigError(
                "reply_tags must be None or (progress tag, outcome tag), each [A-Za-z0-9_-]+"
            )
        if self.runtime is not None and not isinstance(self.runtime, str):
            raise ConfigError("runtime must be None or a string")
        if self.mode is not None:
            object.__setattr__(self, "mode", _member(Mode, "mode", self.mode))
        if not isinstance(self.prompt, str):
            raise ConfigError("prompt must be a string")
        if not isinstance(self.resume, bool):
            raise ConfigError(f"resume must be a bool, got {self.resume!r:.80}")
        if self.resume and self.sid is None:
            raise ConfigError("resume: needs spec.sid, the session to resume")
        if self.events is not None and not (
            isinstance(self.events, str) and self.events.isprintable()
        ):
            raise ConfigError(f"events must be a printable path, got {self.events!r:.80}")


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    unset: tuple[str, ...] = ()
    cwd: str = ""
    transcript: str = ""
    resume: str = ""  # a human's shell command, each interpolated field shlex.quoted

    project: bool | None = None

    def __post_init__(self):
        if self.project is not None and not isinstance(self.project, bool):
            raise ConfigError("launch: project must be a bool or None")
        env = self.env
        for name, ok in (
            ("argv", isinstance(self.argv, tuple) and all(map(_clean_string, self.argv))),
            (
                "env",
                environment_ok(env),
            ),
            ("unset", isinstance(self.unset, tuple) and all(map(_clean_string, self.unset))),
        ):
            if not ok:
                raise ConfigError(f"launch: {name} must be strings without NUL")
        object.__setattr__(self, "env", MappingProxyType(dict(env)))


_IDENTITY = {
    "sid": lambda v: sid_error(v) is None,
    "cwd": printable_abs,
    "transcript": lambda v: v == "" or printable_abs(v),
    "project": lambda v: isinstance(v, bool),
    "resume": lambda v: isinstance(v, str),
}


@dataclass(frozen=True)
class SessionInfo:
    """The one session identity; persisted() and from_fields are its one codec, for run.jsonl's
    session line and session.json's identity keys. Read back from either (agent-writable), it is a
    status hint, never an authorization, a path to open or argv to run without re-validation.
    """

    sid: str
    cwd: str
    project: bool
    transcript: str
    resume: str

    def persisted(self) -> dict:
        return {
            "sid": self.sid,
            "cwd": self.cwd,
            "project": self.project,
            "transcript": self.transcript,
            "resume": self.resume,
        }

    @classmethod
    def from_fields(cls, fields: Mapping, *, strict: bool = False) -> "SessionInfo | None":
        """persisted()'s fields (a missing transcript or resume is ""); invalid: None, or with
        strict a ValueError naming the first bad key.
        """
        found = fields if isinstance(fields, Mapping) else {}
        get = found.get
        for key, ok in _IDENTITY.items():
            if not ok(get(key, "" if key in ("transcript", "resume") else None)):
                if strict:
                    raise ValueError(key)
                return None
        return cls(
            found["sid"], found["cwd"], found["project"], get("transcript", ""), get("resume", "")
        )


@dataclass(frozen=True)
class Prepared:
    """A Mode's start, fixed before it starts: the Spec as started, its SessionInfo, argv and
    child env (in_process, or a tui session attached: none).
    """

    spec: Spec
    info: SessionInfo
    argv: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True)
class ClientBound:
    """An adapter's notice that its runtime bound client_id: a run.jsonl client_session line,
    never a SessionEvent.
    """

    client_id: str


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


SessionEvent = Activity | Reply | TurnEnd | Blocked | LimitHit | Exited


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
    resumable: Resumable  # given its plain value too
    last_active: float | None
    resume_command: str

    def __post_init__(self):
        object.__setattr__(self, "resumable", _member(Resumable, "resumable", self.resumable))


@dataclass(frozen=True)
class Stderr:
    text: str


@dataclass(frozen=True)
class Paste:
    text: str


@dataclass(frozen=True)
class Key:
    name: str


class DriverError(Exception):
    """A failed start, or a call its Mode lacks."""


class NoRecordedSession(DriverError):
    """The requested sid has no durable session record in its workdir."""


class NotDisplayable(DriverError):
    """From a Host that is not Displayable: `host <name> cannot display`."""


@dataclass(frozen=True)
class IdleTimeout:
    seconds: float


@dataclass(frozen=True)
class SessionRow:
    """runtime: a tui session's (HostedSession's), else None; shown: the session showing it, ""
    unnamed, None none; state: given its plain value too.
    """

    name: str
    sid: str | None
    runtime: str | None
    state: ActivityState
    pane: str | None
    opener: str | None
    shown: str | None
    last_lines: str | None
    own: bool

    def __post_init__(self):
        object.__setattr__(self, "state", _member(ActivityState, "state", self.state))


@dataclass(frozen=True)
class Hosts:
    """A parsed [hosts] table (Host.parse_hosts): the default Host's name and each [hosts.<name>],
    a read-only copy.
    """

    default: str
    tables: Mapping[str, Mapping]

    def __post_init__(self):
        object.__setattr__(self, "tables", freeze(self.tables))
