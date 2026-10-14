"""A Mode's running session, and what its Modes share to start one. The class's start() fixes a
Prepared before anything starts, has `record` write the session line, then starts and returns the
session, so none exists unstarted. stop() is idempotent.
"""

import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection
from dataclasses import replace
from typing import ClassVar, Self

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.adapters.common import check_selection
from agent_pm.session.adapters.registry import Registry
from agent_pm.session.engine.types import (
    ClientBound,
    ConfigError,
    Launch,
    Liveness,
    Mode,
    Prepared,
    SessionEvent,
    SessionInfo,
    Spec,
    Stderr,
)
from agent_pm.utils import Display, overlaps


def child_env(launch: Launch, cwd: str, drop: Collection[str] = ()) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in drop and k not in launch.unset}
    return {**env, **launch.env, "PWD": cwd}


def identity_drop(registry: Registry) -> frozenset[str]:
    """Each configured Adapter class's identity_env, and its child_defaults at their value inside
    it; builds no Adapter.
    """
    kinds = [registry.adapter_class(name) for name in registry.configured()]
    drop = {key for kind in kinds for key in kind.identity_env}
    for kind in kinds:
        if kind.session_env and kind.session_env in os.environ:
            drop.update(k for k, v in kind.child_defaults.items() if os.environ.get(k) == v)
    return frozenset(drop)


class RunningMode(ABC):
    kind: ClassVar[Mode]

    def __init__(self, prepared: Prepared) -> None:
        """Built by start (TuiMode.attach too) from what it started."""
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
        host=None,
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
        host=None,
        display: Display = None,
    ) -> Self:
        """The session started; spec.sid set; registry: tui settings and identity_drop; host: the
        selected execution Host; display: a tui session's, kept as given (other Modes refuse one).
        """

    @abstractmethod
    def poll(self, timeout: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        """Events within `timeout` s and whether they ended; raises the Mode's own failures."""

    @abstractmethod
    def returncode(self, timeout: float) -> int | None:
        """None while running; -N: killed by signal N."""

    @abstractmethod
    def state(self) -> Liveness: ...

    @abstractmethod
    def send(self, text: str) -> None: ...

    @abstractmethod
    def screen(self, lines: int | None = None) -> str: ...

    @abstractmethod
    def stop(self) -> None: ...

    @classmethod
    def _unsupported(cls, spec: Spec) -> ConfigError:
        return ConfigError(f"runtime {spec.runtime!r} does not support {cls.kind.value} mode")

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
        if spec.access.commands and adapter.runtime_note:
            return replace(spec, prompt=spec.prompt + "\n\n" + adapter.runtime_note)
        return spec

    @staticmethod
    def check_protection(spec: Spec, adapter: Adapter) -> None:
        if not spec.protect:
            return
        try:
            writable = (
                spec.access.cwd or spec.workdir,
                *spec.access.dirs,
                *adapter.writable_dirs(spec),
            )
            for path in writable:
                for protected in spec.protect:
                    if overlaps(path, protected):
                        raise ConfigError(
                            f"writable path {path} overlaps protected path {protected}"
                        )
        except OSError as error:
            raise ConfigError(f"{spec.runtime}'s writable dirs: {error}") from error

    @staticmethod
    def _launched(spec: Spec, launch: Callable[[Spec], Launch], drop: Collection[str]) -> Prepared:
        if spec.sid is None:
            raise ConfigError("sid: missing")
        command = launch(spec)
        if not command.argv:
            raise ConfigError(f"runtime {spec.runtime!r} gave no command")
        cwd = command.cwd or spec.access.cwd or spec.workdir
        project = spec.access.project if command.project is None else command.project
        info = SessionInfo(spec.sid, cwd, project, command.transcript, command.resume)
        return Prepared(spec, info, command.argv, child_env(command, cwd, drop))
