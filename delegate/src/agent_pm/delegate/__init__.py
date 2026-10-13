"""The Delegate: runs one role as an agent session, its prompt and access from the Composer, its
session from the Driver, read from its replies' progress marks and outcome block.
"""

from agent_pm.delegate.composer import ConfigError as ComposerConfigError
from agent_pm.delegate.composer import ConfigSource, RunConfig, RunOverrides, trusted_dirs
from agent_pm.delegate.composer import index as task_index
from agent_pm.delegate.composer import resolve as resolve_run
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
from agent_pm.delegate.event.types import STATUSES, InvalidOutcome, Outcome

__all__ = [
    "ConfigError",
    "ComposerConfigError",
    "resolve_run",
    "task_index",
    "trusted_dirs",
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
    "ConfigSource",
    "RunOverrides",
    "DelegateEvent",
    "CONFIG",
    "IMPORT_ROOTS",
    "PYTHONPATH",
    "harness_host",
]
