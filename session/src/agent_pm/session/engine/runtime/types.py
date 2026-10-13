"""Driver invocation options and the execution services supplied by its caller."""

from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType

from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes import headless


@dataclass(frozen=True, kw_only=True)
class StartOptions:
    resume: str | None = None
    idle_timeout: float | None = None
    events: str | None = None


@dataclass(frozen=True, kw_only=True)
class DriverServices:
    host: ModuleType = tmux_host
    probe: Callable[..., str | None] = headless.probe
    clock: Callable[[], float] | None = None
