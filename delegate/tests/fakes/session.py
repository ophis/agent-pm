"""A caller-owned fake Session class for Delegate driver contract tests."""

from agent_pm.session import Session, Spec

from session.tests.fakes.session import FakeSession


class SessionClass(FakeSession):
    @classmethod
    def fresh(cls):
        return cls.scripted()

    @classmethod
    def start(cls, spec: Spec, *, display=None, host=None, idle_timeout=None) -> Session:
        return super().start(spec, display=display, host=host, idle_timeout=idle_timeout)
