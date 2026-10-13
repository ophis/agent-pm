"""The session layer: the Driver (engine/api.py, modes/) runs agent sessions through each runtime's
Adapter (adapters/), behind the ABCs Session and Host; `__all__` is its public API.
"""

from agent_pm.session.engine import api
from agent_pm.session.engine.base import Session
from agent_pm.session.engine.status_log import SessionStatusLog
from agent_pm.session.engine.types import (
    Access,
    Activity,
    Blocked,
    ConfigError,
    DriverError,
    Exited,
    IdleTimeout,
    LimitHit,
    Reply,
    SessionBound,
    SessionRow,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
    Window,
)
from agent_pm.session.hosts.base import Displayable, Host, HostProcess, NotDisplayable, Terminal

Driver = api.Driver
Handle = api.Handle
MODES = api.MODES
SIGNALS = api.SIGNALS
holds = api.holds
note = api.note
session_record = api.session_record
sessions = api.sessions
close = api.close

__all__ = [
    *api.__all__,
    "Session",
    "Spec",
    "Access",
    "Host",
    "HostProcess",
    "Terminal",
    "Displayable",
    "NotDisplayable",
    "SessionStatusLog",
    "Usage",
    "Window",
    "SessionRow",
    "SessionStatus",
    "Activity",
    "Blocked",
    "Exited",
    "IdleTimeout",
    "LimitHit",
    "Reply",
    "SessionBound",
    "Stderr",
    "TurnEnd",
    "DriverError",
    "ConfigError",
]
