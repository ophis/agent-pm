"""Runtime, a Mode's running session, and what the Runtimes share to start one. The class's
start() fixes a Prepared before anything starts, has `record` write the session line, then starts
and returns the session, so none exists unstarted. stop() is idempotent.
"""

import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Sequence
from dataclasses import replace
from typing import ClassVar, Self

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.adapters.common import check_selection
from agent_pm.session.adapters.registry import Registry
from agent_pm.session.engine.types import (
    COMMAND,
    ClientBound,
    ConfigError,
    KeyPress,
    Launch,
    Liveness,
    Mode,
    Prepared,
    SessionError,
    SessionEvent,
    SessionInfo,
    Spec,
    Stderr,
)
from agent_pm.session.hosts.base import Host
from agent_pm.utils import LEASE_ENV, Display, command_path

START_SID = "AGENT_PM_SID"

EVENTS = "AGENT_PM_EVENTS"


def child_env(launch: Launch, cwd: str, drop: Collection[str] = ()) -> dict[str, str]:
    """The environment less drop and unset, then the launch env and PWD; PATH led by the running
    plugin's bin/, where PATH finds COMMAND (FR-32), when it does.
    """
    env = {k: v for k, v in os.environ.items() if k not in drop and k not in launch.unset}
    env = {**env, **launch.env, "PWD": cwd}
    command = command_path(COMMAND)
    if command is not None:
        plugin = os.path.dirname(command)
        path = env.get("PATH", os.defpath)
        if path.split(os.pathsep)[0] != plugin:
            env["PATH"] = plugin + os.pathsep + path
    return env


def identity_drop(registry: Registry) -> frozenset[str]:
    """START_SID and EVENTS (a caller's start token and events file, never a nested start's),
    LEASE_ENV (a manager's roster lease), each configured Adapter class's identity_env, and its
    child_defaults at their value inside it; builds no Adapter.
    """
    kinds = [registry.adapter_class(name) for name in registry.configured()]
    drop = {START_SID, EVENTS, *LEASE_ENV, *(key for kind in kinds for key in kind.identity_env)}
    for kind in kinds:
        if kind.session_env and kind.session_env in os.environ:
            drop.update(k for k, v in kind.child_defaults.items() if os.environ.get(k) == v)
    return frozenset(drop)


class Runtime(ABC):
    kind: ClassVar[Mode]

    def __init__(self, prepared: Prepared) -> None:
        """Built by start (TuiRuntime.attach too) from what it started."""
        self._prepared = prepared

    @property
    def prepared(self) -> Prepared:
        return self._prepared

    @classmethod
    def preflight(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        host: Host,
        display: Display = None,
    ) -> Prepared:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def start(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        record: Callable[[SessionInfo], None],
        host: Host,
        display: Display = None,
        check_name: Callable[[str], None] | None = None,
    ) -> Self:
        """The session started; spec.sid set; registry: tui settings and identity_drop; host: the
        selected execution Host; display: a tui session's, kept as given (other Runtimes refuse one).
        """

    @property
    def opening_pending(self) -> bool:
        """A launch input not yet sent (tui only)."""
        return False

    @abstractmethod
    def poll(self, timeout: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        """Events within `timeout` s and whether they ended; raises the Runtime's own failures."""

    @abstractmethod
    def returncode(self, timeout: float) -> int | None:
        """None while running; -N: killed by signal N."""

    @abstractmethod
    def state(self) -> Liveness: ...

    @abstractmethod
    def send(self, text: str) -> None: ...

    @abstractmethod
    def screen(self, lines: int | None = None) -> str: ...

    def press(self, keys: Sequence[KeyPress]) -> None:
        raise SessionError(f"press: a {self.kind.value} session has no terminal")

    @abstractmethod
    def stop(self) -> None: ...

    @classmethod
    def _unsupported(cls, spec: Spec) -> ConfigError:
        return ConfigError(f"client {spec.client!r} does not support {cls.kind.value} mode")

    @staticmethod
    def _no_tui_keys(spec: Spec, display: Display = None) -> None:
        for key, value in (("name", spec.name), ("events", spec.events), ("display", display)):
            if value is not None:
                raise ConfigError(f"{key}: tui mode only")

    @staticmethod
    def _selected(spec: Spec, adapter: Adapter) -> None:
        check_selection(spec.flags, adapter.forbidden_selection, adapter.valued_flags)

    @staticmethod
    def _noted(spec: Spec, adapter: Adapter) -> Spec:
        if spec.access.commands and adapter.client_note:
            return replace(spec, prompt=spec.prompt + "\n\n" + adapter.client_note)
        return spec

    @staticmethod
    def _launched(spec: Spec, launch: Callable[[Spec], Launch], drop: Collection[str]) -> Prepared:
        if spec.sid is None:
            raise ConfigError("sid: missing")
        command = launch(spec)
        if not command.argv:
            raise ConfigError(f"client {spec.client!r} gave no command")
        cwd = command.cwd or spec.access.cwd or spec.workdir
        project = spec.access.project if command.project is None else command.project
        info = SessionInfo(spec.sid, cwd, project, command.transcript, command.resume)
        return Prepared(spec, info, command.argv, child_env(command, cwd, drop))
