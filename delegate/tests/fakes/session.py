"""A Session class with the bundled start contract, ahead of TASK-365's implementation."""

from dataclasses import dataclass

from agent_pm.session import Session
from agent_pm.session import Spec as SessionSpec

from session.tests.fakes.session import FakeSession


@dataclass(frozen=True, kw_only=True)
class StartSpec(SessionSpec):
    runtime: str
    mode: str
    prompt: str
    resume: bool = False
    events: str | None = None


class SessionClass:
    started: list = []

    @classmethod
    def fresh(cls):
        return type(cls.__name__, (cls,), {"started": []})

    @classmethod
    def start(cls, spec: StartSpec, *, display=None, host=None, idle_timeout=None) -> Session:
        cls.started.append((spec, host, display, idle_timeout))
        return FakeSession.scripted().start(
            spec.runtime,
            spec.mode,
            spec.prompt,
            spec,
            resume=spec.sid if spec.resume else None,
            events=spec.events,
            idle_timeout=idle_timeout,
            host=host,
            display=display,
        )
