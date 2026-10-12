"""The session layer's data types; most are adapters/base.py's, re-exported."""

from dataclasses import dataclass

from agent_pm.session.adapters.base import (
    Access,
    Activity,
    Blocked,
    ConfigError,
    Event,
    Exited,
    Key,
    LimitHit,
    Paste,
    Reply,
    SessionBound,
    SessionInfo,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
    Window,
)

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


class DriverError(Exception):
    """A failed start, or a call its Mode lacks."""


@dataclass(frozen=True)
class IdleTimeout:
    seconds: float


@dataclass(frozen=True)
class SessionRow:
    """runtime: a Driver tui session's, else None; shown: the session showing it, "" unnamed, None none."""

    name: str
    sid: str | None
    runtime: str | None
    state: str
    pane: str | None
    opener: str | None
    shown: str | None
    last_lines: str | None
    own: bool
