"""The session layer's data types, used by two or more of its modules. Access.dirs are granted
write, Access.reads only read; Spec.flags/env are caller-supplied for every Mode; times are epoch
seconds.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from agent_pm.session.engine.configuration.types import ConfigError
from agent_pm.utils import printable_abs

__all__ = [
    "Access",
    "Activity",
    "Blocked",
    "ConfigError",
    "DriverError",
    "Event",
    "Exited",
    "IdleTimeout",
    "Key",
    "Launch",
    "LimitHit",
    "Paste",
    "Reply",
    "SessionBound",
    "SessionInfo",
    "SessionRow",
    "SessionStatus",
    "Spec",
    "Stderr",
    "TurnEnd",
    "Usage",
    "Window",
]

EFFORTS = ("low", "medium", "high", "xhigh", "max")
TAG = re.compile(r"[A-Za-z0-9_-]+")


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
                raise ConfigError.string_tuple(name)
        if not env_ok(self.env):
            raise ConfigError.access_environment()


@dataclass(frozen=True, kw_only=True)
class Spec:
    workdir: str
    access: Access = Access()
    tier: int | None = None
    effort: str | None = None
    sid: str | None = None
    flags: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    name: str | None = None
    protect: tuple[str, ...] = ()  # paths no writable dir may overlap (a sandboxing Adapter only)
    reply_tags: tuple[str, str] | None = (
        None  # the caller's (progress, outcome) tags (the prompt-printing Adapter only)
    )

    def __post_init__(self):
        if not printable_abs(self.workdir):
            raise ConfigError.workdir(self.workdir)
        if self.tier is not None and (
            isinstance(self.tier, bool) or not isinstance(self.tier, int) or not 1 <= self.tier <= 4
        ):
            raise ConfigError.tier(self.tier)
        if self.effort is not None and self.effort not in EFFORTS:
            raise ConfigError.effort(EFFORTS, self.effort)
        if self.access.cwd != "" and not printable_abs(self.access.cwd):
            raise ConfigError.cwd(self.access.cwd)
        if self.sid is not None and (error := sid_error(self.sid)):
            raise ConfigError(error)
        if not (isinstance(self.flags, tuple) and all(map(_clean, self.flags))):
            raise ConfigError.flags()
        if not env_ok(self.env):
            raise ConfigError.environment()
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        if self.name is not None and not isinstance(self.name, str):
            raise ConfigError.name_type()
        if not (isinstance(self.protect, tuple) and all(map(printable_abs, self.protect))):
            raise ConfigError.protected_paths()
        if self.reply_tags is not None and not (
            isinstance(self.reply_tags, tuple)
            and len(self.reply_tags) == 2
            and all(isinstance(t, str) and TAG.fullmatch(t) for t in self.reply_tags)
        ):
            raise ConfigError.reply_tags()


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
            raise ConfigError.launch_project()
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
                raise ConfigError.launch_strings(name)


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


class DriverError(Exception):
    """A failed start, or a call its Mode lacks."""

    @classmethod
    def sessions_unavailable(cls, cause: Exception | None = None) -> "DriverError":
        detail = "tmux failed" if cause is None else str(cause)
        return cls(f"tmux sessions unknown: {detail}")


@dataclass(frozen=True)
class IdleTimeout:
    seconds: float


@dataclass(frozen=True)
class SessionRow:
    """runtime: a Driver tui session's, else None; shown: the session showing it, "" unnamed, None
    none.
    """

    name: str
    sid: str | None
    runtime: str | None
    state: str
    pane: str | None
    opener: str | None
    shown: str | None
    last_lines: str | None
    own: bool
