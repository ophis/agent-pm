"""The Delegate: runs one role as an agent session, its prompt and access from the Composer, its session from the
Driver, read from its replies' progress marks and outcome block."""

from agent_pm.delegate.composer import RunConfig
from agent_pm.delegate.engine.base import Delegate
from agent_pm.delegate.engine.plan import ConfigError, Plan, plan
from agent_pm.delegate.engine.runner import (
    IDLE_TIMEOUT,
    Begun,
    Finished,
    Limit,
    Progress,
    Result,
    run,
)
from agent_pm.delegate.event import DelegateEvent
from agent_pm.delegate.event.outcome import STATUSES, InvalidOutcome, Outcome

__all__ = [
    "ConfigError",
    "Plan",
    "plan",
    "run",
    "Result",
    "Limit",
    "Outcome",
    "InvalidOutcome",
    "Begun",
    "Progress",
    "Finished",
    "IDLE_TIMEOUT",
    "STATUSES",
    "Delegate",
    "RunConfig",
    "DelegateEvent",
]
