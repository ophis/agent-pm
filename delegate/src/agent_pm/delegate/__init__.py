"""The Delegate: runs one role as an agent session, its prompt and access from the Composer, its session from the
Driver, read from its replies' progress marks and outcome block."""

from agent_pm.delegate.composer import RunConfig
from agent_pm.delegate.engine.base import Delegate
from agent_pm.delegate.engine.config import CONFIG, IMPORT_ROOTS, PYTHONPATH, harness_host
from agent_pm.delegate.engine.plan import configure, plan
from agent_pm.delegate.engine.runner import IDLE_TIMEOUT, run
from agent_pm.delegate.engine.types import (
    Begun,
    ConfigError,
    Event,
    Finished,
    Limit,
    Plan,
    Progress,
    Result,
    Spec,
)
from agent_pm.delegate.event import DelegateEvent
from agent_pm.delegate.event.outcome import STATUSES, InvalidOutcome, Outcome

__all__ = [
    "ConfigError",
    "Spec",
    "Plan",
    "configure",
    "plan",
    "run",
    "Result",
    "Limit",
    "Outcome",
    "InvalidOutcome",
    "Begun",
    "Progress",
    "Finished",
    "Event",
    "IDLE_TIMEOUT",
    "STATUSES",
    "Delegate",
    "RunConfig",
    "DelegateEvent",
    "CONFIG",
    "IMPORT_ROOTS",
    "PYTHONPATH",
    "harness_host",
]
