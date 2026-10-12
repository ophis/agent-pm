"""The Delegate: runs one role as an agent session, its prompt and access from the Composer, its session from the
Driver, read from its replies' progress marks and outcome block. Every check runs first (plan): a refusal is
ConfigError, nothing started or written. Event: the callback line, not an adapter's or runner's event."""

from agent_pm.role.composer import RunConfig
from agent_pm.role.contract import Event
from agent_pm.role.delegate.base import Delegate
from agent_pm.role.delegate.outcome import STATUSES, InvalidOutcome, Outcome
from agent_pm.role.delegate.plan import ConfigError, Plan, plan
from agent_pm.role.delegate.runner import (
    IDLE_TIMEOUT,
    Begun,
    Finished,
    Limit,
    Progress,
    Result,
    run,
)

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
    "Event",
]
