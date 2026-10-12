"""The Delegate: runs one role as an agent session, its prompt and access from the Composer, its session from the
Driver, read from its replies' progress marks and outcome block. Every check runs first (plan): a refusal is
ConfigError, nothing started or written."""

from agent_pm.delegate.outcome import STATUSES, InvalidOutcome, Outcome
from agent_pm.delegate.plan import ConfigError, Plan, plan
from agent_pm.delegate.runner import IDLE_TIMEOUT, Begun, Finished, Limit, Progress, Result, run

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
]
