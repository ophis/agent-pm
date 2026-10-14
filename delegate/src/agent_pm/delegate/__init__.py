"""The Delegate: runs one role as an agent session, its prompt and access from the Composer, its
session from the Driver, read from its replies' progress marks and outcome block.
"""

import os

from agent_pm.delegate.composer import ConfigError as ComposerConfigError
from agent_pm.delegate.composer import RunConfig, trusted_dirs
from agent_pm.delegate.composer import index as task_index
from agent_pm.delegate.composer import resolve as resolve_run
from agent_pm.delegate.config import (
    CONFIG,
    harness_host,
    read_config,
)
from agent_pm.delegate.engine.base import Delegate
from agent_pm.delegate.engine.config import module_roots, package_dirs
from agent_pm.delegate.engine.drivers import AgentDriver, Driver
from agent_pm.delegate.engine.plan import configure, plan
from agent_pm.delegate.engine.runner import execute
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
from agent_pm.delegate.event.types import STATUSES, InvalidOutcome, Outcome, OutcomeStatus
from agent_pm.session import Driver as SessionDriver

PYTHONPATH: str = os.pathsep.join(module_roots())


def run(run: RunConfig, spec: Spec, *, on_event=None, driver: Driver | SessionDriver) -> Result:
    owner_driver = AgentDriver(driver.session_cls) if isinstance(driver, SessionDriver) else driver
    return execute(plan(run, spec, driver=owner_driver), driver=owner_driver, on_event=on_event)


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
    "Driver",
    "DriverSpec",
    "Instruction",
    "Limit",
    "Outcome",
    "InvalidOutcome",
    "Begun",
    "Progress",
    "Finished",
    "Event",
    "STATUSES",
    "OutcomeStatus",
    "Delegate",
    "RunConfig",
    "DelegateEvent",
    "CONFIG",
    "PYTHONPATH",
    "harness_host",
    "module_roots",
    "package_dirs",
    "read_config",
]
