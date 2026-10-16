"""The Delegate: runs one role as an agent session, its prompt and access from the Composer, its
session from the Driver, read from its replies' progress marks and outcome block.
"""

from agent_pm.delegate.composer import RunConfig
from agent_pm.delegate.engine.base import Delegate
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.delegate.engine.types import ConfigError, Progress, Spec
from agent_pm.delegate.event import DelegateEvent
from agent_pm.delegate.event.types import Outcome
from agent_pm.session import Resumable, SessionError, Window

__all__ = [
    "ConfigError",
    "Spec",
    "SessionError",
    "Resumable",
    "Window",
    "Outcome",
    "Progress",
    "Delegate",
    "HarnessDelegate",
    "RunConfig",
    "DelegateEvent",
]
