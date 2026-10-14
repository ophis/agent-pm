"""The session layer: Session (engine/base.py), whose one concrete class is HostedSession
(engine/hosted.py, modes/), runs agent sessions through each runtime's Adapter (adapters/) on a Host
(hosts/); `__all__` is its API for Session users. Driver, Handle, close, sessions, note and
session_record are deprecated forwarding wrappers (engine/forwarding.py).
"""

from agent_pm.session.engine.base import Session
from agent_pm.session.engine.config import read_config
from agent_pm.session.engine.forwarding import (
    Driver,
    Handle,
    close,
    note,
    session_record,
    sessions,
)
from agent_pm.session.engine.records import RunLog
from agent_pm.session.engine.status_log import SessionStatusLog
from agent_pm.session.engine.types import (
    Access,
    Activity,
    ActivityState,
    Blocked,
    ConfigError,
    DriverError,
    Effort,
    Exited,
    Hosts,
    IdleTimeout,
    LimitHit,
    Liveness,
    Mode,
    NotDisplayable,
    Reply,
    Resumable,
    SessionEvent,
    SessionInfo,
    SessionRow,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
    Window,
)
from agent_pm.session.hosts.base import (
    SIGNALS,
    Displayable,
    Host,
    HostProcess,
    Terminal,
)

__all__ = [
    "read_config",
    "Driver",
    "Handle",
    "SIGNALS",
    "note",
    "session_record",
    "sessions",
    "close",
    "Session",
    "Spec",
    "Access",
    "Mode",
    "Liveness",
    "Effort",
    "Resumable",
    "ActivityState",
    "RunLog",
    "SessionInfo",
    "SessionEvent",
    "Hosts",
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
    "Stderr",
    "TurnEnd",
    "DriverError",
    "ConfigError",
]
