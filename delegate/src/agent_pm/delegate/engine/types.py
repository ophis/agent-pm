"""The Delegate's shared types."""

from dataclasses import dataclass

from agent_pm.delegate.composer import RunConfig, RunParams
from agent_pm.delegate.event.outcome import Outcome
from agent_pm.session import api


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Plan:
    """`out` None: nothing is saved."""

    runtime: str
    mode: str
    sid: str
    resume: bool
    prompt: str
    spec: api.Spec
    run: RunConfig
    params: RunParams
    out: str | None
    name: str | None
    events: str | None


@dataclass(frozen=True)
class Limit:
    """resets_at: epoch seconds, None when not given."""

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
