"""The Delegate: runs one role as an agent session, its prompt and access from the Composer, its
session from the Driver, read from its replies' progress marks and outcome block.
"""

import contextlib
import os
from collections.abc import Iterator, Mapping

from agent_pm.delegate.composer import Composer, RunConfig, trusted_dirs
from agent_pm.delegate.composer import ConfigError as ComposerConfigError
from agent_pm.delegate.composer import index as task_index
from agent_pm.delegate.composer import resolve as resolve_run
from agent_pm.delegate.config import (
    CONFIG,
    harness_host,
    read_config,
)
from agent_pm.delegate.engine import plan as planning
from agent_pm.delegate.engine.base import Delegate
from agent_pm.delegate.engine.config import module_roots, package_dirs
from agent_pm.delegate.engine.drivers import AgentDriver, Driver
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
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import Driver as SessionDriver
from agent_pm.session import Host
from agent_pm.session import read_config as read_session_config
from agent_pm.utils import LOCAL

PYTHONPATH: str = os.pathsep.join(module_roots())


def configure(*, local: str = LOCAL, cfg: Mapping | None = None) -> SessionDriver:
    """The session's Driver from its config, [hosts] parsed once, keeping harness_host (`cfg`, else
    read) once it names one of those hosts.
    """
    with _config("session"):
        table = read_session_config(local=local)
        hosts = Host.parse_hosts(table.get("hosts"))
    with _config("delegate"):
        harness = harness_host(read_config(local=local) if cfg is None else cfg, hosts.tables)
    with _config("session"):
        return SessionDriver.from_config(table, hosts=hosts, harness_host=harness)


@contextlib.contextmanager
def _config(name: str) -> Iterator[None]:
    try:
        yield
    except (OSError, ValueError, SessionConfigError) as e:
        raise ConfigError(f"{name} config: {e}") from None


def _owned(driver: Driver | SessionDriver) -> Driver:
    return AgentDriver(driver.session_cls) if isinstance(driver, SessionDriver) else driver


def plan(
    run: RunConfig,
    spec: Spec,
    *,
    driver: Driver | SessionDriver,
    composition: Composer | None = None,
) -> Plan:
    return planning.plan(run, spec, driver=_owned(driver), composition=composition)


def run(run: RunConfig, spec: Spec, *, on_event=None, driver: Driver | SessionDriver) -> Result:
    """A session run makes its workdir; a refused start removes it again while still empty."""
    owner_driver = _owned(driver)
    accepted = planning.plan(run, spec, driver=owner_driver)
    if not owner_driver.session_based:
        return execute(accepted, driver=owner_driver, on_event=on_event)
    workdir = accepted.spec.workdir
    made = not os.path.isdir(workdir)
    os.makedirs(workdir, exist_ok=True)
    try:
        return execute(accepted, driver=owner_driver, on_event=on_event)
    except ConfigError:
        if made:
            with contextlib.suppress(OSError):
                os.rmdir(workdir)
        raise


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
