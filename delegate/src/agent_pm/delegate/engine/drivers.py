"""Driver starts a bundled session spec or delivers its instruction. The injected Session.start
owns placement and idle timeout; today's runner retains api.start pending TASK-365.
"""

import os
from abc import ABC, abstractmethod

from agent_pm.delegate.engine.types import ConfigError, Instruction
from agent_pm.session import Session
from agent_pm.utils import Display, save


class Driver(ABC):
    @abstractmethod
    def start(
        self,
        spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Session | Instruction: ...


class AgentDriver(Driver):
    def __init__(self, session_cls):
        self.session_cls = session_cls

    def start(
        self,
        spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Session:
        return self.session_cls.start(spec, display=display, host=host, idle_timeout=idle_timeout)


class InstructionDriver(Driver):
    def __init__(self, out: str):
        self.path = os.path.abspath(out)

    def start(
        self,
        spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Instruction:
        if spec.resume:
            raise ConfigError("instruction has no session to resume")
        if host is not None or display is not None:
            raise ConfigError("instruction has no host or display")
        if idle_timeout is not None:
            raise ConfigError("instruction has no idle timeout")
        return self.deliver(spec.prompt)

    def deliver(self, prompt: str) -> Instruction:
        """The current runner supplies prompt directly until session.Spec gains its prompt field."""
        save(self.path, prompt)
        return Instruction(self.path)

    def detach(self) -> None:
        raise ConfigError("instruction cannot detach")
