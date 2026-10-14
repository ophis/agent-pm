"""Driver delivers an instruction or starts a Session through its injected class."""

import os
from abc import ABC, abstractmethod

from agent_pm.delegate.engine.types import ConfigError, DriverSpec, Instruction
from agent_pm.session import Session, Spec
from agent_pm.utils import Display, save


class Driver(ABC):
    @abstractmethod
    def start(
        self,
        spec: DriverSpec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Session | Instruction: ...


class AgentDriver(Driver):
    def __init__(self, session_cls: type[Session]):
        self.session_cls = session_cls

    def start(
        self,
        spec: DriverSpec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Session:
        return self.session_cls.start(
            spec.session, display=display, host=host, idle_timeout=idle_timeout
        )

    def writable_dirs(self, spec: Spec) -> tuple[str, ...]:
        return self.session_cls.writable_dirs(spec)


class InstructionDriver(Driver):
    def start(
        self,
        spec: DriverSpec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> Instruction:
        session = spec.session
        if session.resume:
            raise ConfigError("instruction has no session to resume")
        if host is not None or display is not None:
            raise ConfigError("instruction has no host or display")
        if idle_timeout is not None:
            raise ConfigError("instruction has no idle timeout")
        path = os.path.abspath(spec.out)
        save(path, session.prompt)
        return Instruction(path)

    def detach(self) -> None:
        raise ConfigError("instruction cannot detach")
