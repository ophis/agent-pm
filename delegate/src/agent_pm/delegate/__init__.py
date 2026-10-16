"""The Delegate: runs one role as an agent session, its prompt and access from the Composer, its
session from the Driver, read from its replies' progress marks and outcome block.
"""

from agent_pm.delegate.composer import ConfigError as ComposerConfigError
from agent_pm.delegate.composer import RunConfig
from agent_pm.delegate.config import read_config
from agent_pm.delegate.engine.base import Delegate
from agent_pm.delegate.engine.config import module_roots
from agent_pm.delegate.engine.drivers import Driver
from agent_pm.delegate.engine.types import (
    Begun,
    ConfigError,
    DriverSpec,
    Event,
    Finished,
    Instruction,
    Limit,
    Plan,
    Progress,
    Result,
    Spec,
)
from agent_pm.delegate.event import DelegateEvent
from agent_pm.delegate.event.types import InvalidOutcome, Outcome
from agent_pm.session import NotShowable, Resumable, SessionError, SessionStatus, Usage, Window
from agent_pm.utils import OutcomeStatus

__all__ = [
    "ConfigError",
    "ComposerConfigError",
    "Spec",
    "Plan",
    "Result",
    "Driver",
    "SessionError",
    "NotShowable",
    "Resumable",
    "SessionStatus",
    "Usage",
    "Window",
    "DriverSpec",
    "Instruction",
    "Limit",
    "Outcome",
    "InvalidOutcome",
    "Begun",
    "Progress",
    "Finished",
    "Event",
    "OutcomeStatus",
    "Delegate",
    "RunConfig",
    "DelegateEvent",
    "module_roots",
    "read_config",
]
