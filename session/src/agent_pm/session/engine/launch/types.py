"""Invocation context and production strategies that prepare and start each session mode."""

import contextlib
import os
from abc import ABC, abstractmethod
from collections.abc import Collection, Iterator
from dataclasses import dataclass, replace
from types import ModuleType

from agent_pm.session.adapters import types as registry
from agent_pm.session.adapters.types import Adapter, Headless, InProcess, Tui
from agent_pm.session.engine.runtime.validation import validate_events, validate_name
from agent_pm.session.engine.types import (
    ConfigError,
    DriverError,
    Launch,
    SessionInfo,
    SessionRequest,
    Spec,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes.headless import types as headless
from agent_pm.session.modes.in_process import types as in_process
from agent_pm.session.modes.tui import types as tui


@dataclass(frozen=True, kw_only=True)
class LaunchContext:
    runtime: str
    spec: Spec
    events: str | None
    registry: registry.Registry
    host: ModuleType = tmux_host


def child_env(launch: Launch, cwd: str, drop: Collection[str] = ()) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in drop and k not in launch.unset}
    return {**env, **launch.env, "PWD": cwd}


def _identity_drop(registry: registry.Registry) -> frozenset[str]:
    """Each Adapter's identity_env, and its child_defaults at their value inside it."""
    adapters = [registry.adapter(name) for name in registry.configured()]
    drop = {key for a in adapters for key in a.identity_env}
    for a in adapters:
        if a.session_env and a.session_env in os.environ:
            drop.update(k for k, v in a.child_defaults.items() if os.environ.get(k) == v)
    return frozenset(drop)


def _no_tui_keys(name, events) -> None:
    for key, value in (("name", name), ("events", events)):
        if value is not None:
            raise ConfigError.tui_option(key)


def _no_tui_fields(spec: Spec) -> Spec:
    if spec.protect:
        raise ConfigError.protect_mode()
    return spec


class _LaunchSteps(ABC):
    """A Mode starting the command its Adapter's launch gives; info() keeps that Launch and its cwd
    for begin().
    """

    runtime: str

    @abstractmethod
    def launch(self, prompt: str, request: SessionRequest) -> Launch: ...

    def info(self, prompt: str, request: SessionRequest) -> SessionInfo:
        spec = request.spec
        assert spec.sid is not None
        self.command = launch = self.launch(prompt, request)
        if not launch.argv:
            raise ConfigError.empty_command(self.runtime)
        self.cwd = launch.cwd or spec.access.cwd or spec.workdir
        return SessionInfo(
            spec.sid,
            "",
            self.cwd,
            spec.access.project if launch.project is None else launch.project,
            launch.transcript,
            launch.resume,
        )


class _HeadlessSteps(_LaunchSteps):
    name = None

    def specced(self, spec: Spec) -> Spec:
        self.drop = _identity_drop(self.registry)
        return _no_tui_fields(spec)

    @staticmethod
    def run_of(adapter: Adapter) -> Headless | None:
        return adapter.headless()

    def __init__(self, run: Headless, context: LaunchContext):
        self.registry = context.registry
        _no_tui_keys(context.spec.name, context.events)
        self.run, self.runtime = run, context.runtime

    def launch(self, prompt: str, request: SessionRequest) -> Launch:
        return self.run.launch(prompt, request)

    def prepared(
        self, _info: SessionInfo, _workdir: str, /
    ) -> contextlib.AbstractContextManager[headless.HeadlessMode]:
        return contextlib.nullcontext(headless.HeadlessMode(self.run))

    def begin(self, host: headless.HeadlessMode) -> None:
        launch = self.command
        try:
            host.begin(launch.argv, cwd=self.cwd, env=child_env(launch, self.cwd, self.drop))
        except (OSError, ValueError) as e:
            raise DriverError.start_command(launch.argv[0], e) from e


class _TuiSteps(_LaunchSteps):
    @staticmethod
    def run_of(adapter: Adapter) -> Tui | None:
        return adapter.tui()

    def __init__(self, run: Tui, context: LaunchContext):
        self.registry = context.registry
        self.run, self.runtime, self.settings = run, context.runtime, context.registry.tui()
        self.host = context.host
        spec = context.spec
        assert spec.sid is not None
        self.name = validate_name(
            f"{context.runtime}-{spec.sid[:8]}" if spec.name is None else spec.name
        )
        self.events = validate_events(context.events, host=self.host)
        self.tmux = self.host.tmux_path()
        if self.tmux is None:
            raise DriverError.missing_tmux()

    def specced(self, spec: Spec) -> Spec:
        self.drop = _identity_drop(self.registry)
        hooks = (
            self.tmux,
            *([self.events] if self.events else []),
            os.path.expanduser(tui.TUI_DIR),
        )
        return replace(spec, name=self.name, protect=(*spec.protect, *hooks))

    def launch(self, prompt: str, request: SessionRequest) -> Launch:
        return self.run.launch(prompt, request, tui.hook_argv(tui.session_dir(self.name)))

    @contextlib.contextmanager
    def prepared(self, info: SessionInfo, workdir: str) -> Iterator[tui.TuiMode]:
        with tui.lock(self.name):
            yield tui.TuiMode.prepare(
                self.name,
                self.run,
                info,
                setup=tui.TuiSetup(
                    events=self.events, runtime=self.runtime, workdir=workdir, tmux=self.tmux
                ),
                host=self.host,
            )

    def begin(self, host: tui.TuiMode) -> None:
        launch = self.command
        host.begin(
            launch.argv,
            cwd=self.cwd,
            env=child_env(launch, self.cwd, self.drop),
            status_line=self.settings.status_line,
        )


class _InProcessSteps:
    name = None
    specced = staticmethod(_no_tui_fields)

    @staticmethod
    def run_of(adapter: Adapter) -> InProcess | None:
        return adapter.in_process()

    def __init__(self, run: InProcess, context: LaunchContext):
        _no_tui_keys(context.spec.name, context.events)
        self.run = run

    def info(self, prompt: str, request: SessionRequest) -> SessionInfo:
        spec = request.spec
        assert spec.sid is not None
        self.run.check(request)
        self.args = prompt, request
        return SessionInfo(
            spec.sid, "", spec.access.cwd or spec.workdir, spec.access.project, "", ""
        )

    def prepared(
        self, _info: SessionInfo, _workdir: str, /
    ) -> contextlib.AbstractContextManager[in_process.InProcessMode]:
        return contextlib.nullcontext(in_process.InProcessMode(self.run))

    def begin(self, host: in_process.InProcessMode) -> None:
        host.begin(*self.args)


STEPS = {"headless": _HeadlessSteps, "tui": _TuiSteps, "in_process": _InProcessSteps}
