"""The Delegate's shared types."""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

from agent_pm.delegate.composer import ConfigError, RunConfig, RunParams, Team
from agent_pm.delegate.composer.types import DELIVERABLE
from agent_pm.delegate.event.types import Outcome
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import Stopped as Stopped

__all__ = [
    "ConfigError",
    "Spec",
    "Plan",
    "Limit",
    "Result",
    "Instruction",
    "Begun",
    "Progress",
    "Finished",
    "Event",
]


class Outer(TypedDict, total=False):
    kind: str
    ctl: Sequence[str]
    resume: Sequence[str]


@dataclass(frozen=True)
class Spec:
    """One run to start: `resume` resumes session `sid`; client None: the role's; `out` None:
    nothing is saved; `team` None: the built-in one. name and events: tui only for plan();
    HarnessDelegate takes them in every mode (`name`: its agent session, `<name>-drive` its
    harness's; `events`: also the detached harness's outcome line).
    """

    input: str
    workdir: str
    task: str | None = None
    mode: str = "headless"
    client: str | None = None
    sid: str | None = None
    resume: bool = False
    name: str | None = None
    events: str | None = None
    out: str | None = None
    repo: str | None = None
    cwd: str | None = None
    team: Team | None = None


@dataclass(frozen=True)
class DriverSpec:
    session: SessionSpec
    out: str


@dataclass(frozen=True)
class Plan:
    """`spec` resolved: its client, sid and team set, its paths absolute; `session` the session's
    Spec.
    """

    spec: Spec
    session: SessionSpec
    run: RunConfig

    @property
    def prompt(self) -> str:
        return self.session.prompt

    @property
    def params(self) -> RunParams:
        return RunParams(
            input=self.spec.input,
            out=self.spec.out or self.driver_spec.out,
            workdir=self.spec.workdir,
            resume=self.spec.resume,
        )

    @property
    def driver_spec(self) -> DriverSpec:
        return DriverSpec(
            self.session, self.spec.out or os.path.join(self.spec.workdir, DELIVERABLE)
        )

    @property
    def client(self) -> str:
        return self.spec.client or self.run.client

    @property
    def sid(self) -> str:
        sid = self.spec.sid
        if sid is None:
            raise ConfigError("plan has no session id")
        return sid


@dataclass(frozen=True)
class Preview:
    """A construction's plan and placement; agent None outside tui mode."""

    plan: Plan
    agent: str | None
    harness: str
    host: str
    harness_host: str
    callback: tuple[str, ...] | None


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
    instruction: str | None = None


@dataclass(frozen=True)
class Instruction:
    path: str


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
