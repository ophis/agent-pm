"""Driver delivers an instruction or starts a Session through its injected class."""

import os
from abc import ABC, abstractmethod

from agent_pm.delegate.engine import config
from agent_pm.delegate.engine.types import ConfigError, DriverSpec, Instruction
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import Session, Spec
from agent_pm.utils import Display, save


class Driver(ABC):
    session_based = True

    def policy(self, runtime: str) -> tuple[tuple[str, ...], dict[str, str]]:
        return (), {}

    def writable_dirs(self, spec: Spec) -> tuple[str, ...]:
        return ()

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

    def policy(self, runtime: str) -> tuple[tuple[str, ...], dict[str, str]]:
        try:
            flags, env = config.policy(runtime)
            self.session_cls.validate_policy(flags=tuple(flags), env=env)
            return tuple(flags), dict(env)
        except (OSError, ValueError, SessionConfigError) as error:
            raise ConfigError(f"policy.{runtime}: {error}") from None

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
    session_based = False

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
