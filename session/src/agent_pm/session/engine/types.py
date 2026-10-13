"""The session layer's data types, used by two or more of its modules. Access.dirs are granted
write, Access.reads only read; Spec.flags/env are caller-supplied for every Mode; times are epoch
seconds.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from agent_pm.utils import printable_abs, sid_error

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


class ConfigError(Exception):
    pass


EFFORTS = ("low", "medium", "high", "xhigh", "max")
TAG = re.compile(r"[A-Za-z0-9_-]+")


def _string_tuple(value) -> bool:
    return isinstance(value, tuple) and all(isinstance(s, str) for s in value)


def _clean_string(value) -> bool:
    return isinstance(value, str) and "\0" not in value


def _env_ok(env) -> bool:
    return isinstance(env, Mapping) and all(
        isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and _clean_string(v)
        for k, v in env.items()
    )


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
        if not _env_ok(self.env):
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
    name: str | None = None
    protect: tuple[str, ...] = ()  # paths no writable dir may overlap (a sandboxing Adapter only)
    reply_tags: tuple[str, str] | None = (
        None  # the caller's (progress, outcome) tags (the prompt-printing Adapter only)
    )

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
        if not (isinstance(self.flags, tuple) and all(map(_clean_string, self.flags))):
            raise ConfigError("flags must be a tuple of strings without NUL")
        if not _env_ok(self.env):
            raise ConfigError("env must map variable names to strings without NUL")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
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
                isinstance(env, Mapping)
                and all(_clean_string(k) and _clean_string(v) for k, v in env.items()),
            ),
            ("unset", isinstance(self.unset, tuple) and all(map(_clean_string, self.unset))),
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
