"""The Delegate's shared types."""

from dataclasses import dataclass, field

from agent_pm.delegate.composer import ConfigError, RunConfig, RunParams, Team
from agent_pm.delegate.event.types import Outcome
from agent_pm.session import Driver
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import Stopped as Stopped

__all__ = [
    "ConfigError",
    "Spec",
    "Plan",
    "Limit",
    "Result",
    "Begun",
    "Progress",
    "Finished",
    "Event",
]


@dataclass(frozen=True)
class Spec:
    """One run to start: `resume` resumes session `sid`; runtime None: the role's; `out` None:
    nothing is saved; `team` None: the built-in one. name and events: tui only.
    """

    input: str
    workdir: str
    task: str | None = None
    mode: str = "headless"
    runtime: str | None = None
    sid: str | None = None
    resume: bool = False
    name: str | None = None
    events: str | None = None
    out: str | None = None
    repo: str | None = None
    cwd: str | None = None
    team: Team | None = None
    protect: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    """`spec` resolved: its runtime, sid and team set, its paths absolute; `session` the session's
    Spec.
    """

    spec: Spec
    session: SessionSpec
    prompt: str
    run: RunConfig
    params: RunParams
    driver: Driver = field(compare=False, repr=False)

    @property
    def runtime(self) -> str:
        return self.spec.runtime or self.run.runtime

    @property
    def sid(self) -> str:
        sid = self.spec.sid
        if sid is None:
            raise ConfigError("plan has no session id")
        return sid


@dataclass(frozen=True)
class Limit:
    """resets_at: epoch seconds or None."""

    resets_at: float | None


@dataclass(frozen=True)
class Result:
    returncode: int
    outcome: Outcome | None
    error: str = ""
    limit: Limit | None = None


@dataclass(frozen=True)
class Begun:
    sid: str


@dataclass(frozen=True)
class Progress:
    name: str
    text: str


@dataclass(frozen=True)
class Finished:
    result: Result


Event = Begun | Progress | Finished
