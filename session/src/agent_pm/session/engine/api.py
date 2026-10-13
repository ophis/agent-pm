"""The Driver's API. start() checks everything, writes a session's `session` record to
<workdir>/run.jsonl, then starts it in its Mode (headless, tui or in_process) and returns its
Handle. Handle.events() streams its events, Stderr and IdleTimeout (none while Blocked; it stops
nothing) up to the Driver's own Exited, recording `client_session` and `stderr`. usage(),
session_status() and session_record() give a runtime's Usage, a session's SessionStatus and its
record; note() appends a record, holds() checks a file's text; writable_dirs() the dirs a runtime
grants itself write to under a Spec. attach(), sessions(), show() and close() reach a tui session by
name; detach() runs a command in a tmux session of its own.
"""

import contextlib
import logging
import os
import re
import signal
import stat
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import replace
from typing import Protocol

from agent_pm.session.adapters import types as registry
from agent_pm.session.adapters.selection import check as check_selection
from agent_pm.session.adapters.types import Adapter, Headless, InProcess, Tui
from agent_pm.session.engine import records
from agent_pm.session.engine.records import holds, session_record
from agent_pm.session.engine.runtime.types import DriverServices, StartOptions
from agent_pm.session.engine.types import (
    Blocked,
    ConfigError,
    DriverError,
    Event,
    Exited,
    IdleTimeout,
    Launch,
    Reply,
    SessionBound,
    SessionInfo,
    SessionRequest,
    SessionRow,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
    sid_error,
)
from agent_pm.session.hosts.tables import NAME
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes.headless import types as headless
from agent_pm.session.modes.in_process import types as in_process
from agent_pm.session.modes.tui import types as tui
from agent_pm.utils import STATE, printable_abs

__all__ = [
    "Driver",
    "Handle",
    "MODES",
    "SIGNALS",
    "holds",
    "note",
    "session_record",
]

MODES = ("headless", "tui", "in_process")
STATES = ("live", "absent", "unknown")
POLL = 0.5
PROBE_DIR = f"{STATE}/probe"
PROBE_TIMEOUT = 120
SIGNALS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
LOG = logging.getLogger("driver")


class Mode(Protocol):
    """A running session as one mode hosts it; a Handle's only way to it. Its own failure raises
    from poll (headless: its parse); the session's is its returncode (an in_process run that raised:
    1).
    """

    def poll(self, timeout: float) -> tuple[list[Event | Stderr], bool]:
        """The events that came, waiting up to `timeout` s for one, and whether no more will come."""
        ...

    def returncode(self, timeout: float) -> int | None:
        """The exit status, waiting at most `timeout` s; None while it runs; negative: killed by
        that signal.
        """
        ...

    def state(self) -> str:
        """One of STATES."""
        ...

    def send(self, text: str) -> None:
        """Submits `text` to the session."""
        ...

    def screen(self, lines: int | None = None) -> str:
        """The session's visible text."""
        ...

    def stop(self) -> None:
        """Ends the session; idempotent."""
        ...


class Handle:
    def __init__(
        self,
        runtime: str,
        mode: str,
        spec: Spec,
        *,
        host: Mode,
        info: SessionInfo,
        idle_timeout: float | None,
        reply: str | None = None,
        blocked: bool = False,
        adapter: Adapter | None = None,
        driver: "Driver | None" = None,
        clock=None,
    ):
        """spec: its sid, name (a tui session's) and workdir."""
        self.sid, self.runtime, self.mode, self.name = spec.sid, runtime, mode, spec.name
        self._host, self._info, self._idle = host, info, idle_timeout
        self._adapter = adapter
        self._driver = driver
        self.clock = time.monotonic if clock is None else clock
        self._channel = records.path(spec.workdir)
        self._reply = reply
        self._queue: deque = deque()
        self._closed = False
        self._ended = False
        self._blocked = blocked

    def events(self) -> Iterator[Event | Stderr | IdleTimeout]:
        last = self.clock()
        try:
            while not self._ended:
                if self._queue:
                    event = self._queue.popleft()
                    self._record(event)
                    last = self.clock()
                    self._blocked = (
                        isinstance(event, Blocked) or self._blocked and isinstance(event, Stderr)
                    )
                    yield event
                    continue
                wait = POLL
                if self._idle is not None and not self._blocked:
                    wait = min(POLL, last + self._idle - self.clock())
                if self._closed:
                    code = self._host.returncode(max(wait, 0))
                    if code is not None:
                        self._ended = True
                        yield Exited(code)
                        continue
                elif wait > 0:
                    found, self._closed = self._host.poll(wait)
                    self._queue.extend(e for e in found if not isinstance(e, Exited))
                    continue
                if wait <= 0 and self._idle is not None:
                    last = self.clock()
                    yield IdleTimeout(self._idle)
        except GeneratorExit:  # the caller closed this iterator; the session goes on
            raise
        except BaseException:
            self._ended = True
            self._host.stop()
            raise

    def _record(self, event: Event | Stderr) -> None:
        if isinstance(event, SessionBound):
            records.note(
                self._channel,
                "client_session",
                sid=self.sid,
                client_id=event.native_id,
                durable=True,
            )
            self._info = replace(self._info, native_id=event.native_id)
            self._refresh_metadata()
        elif isinstance(event, Stderr):
            records.note(self._channel, "stderr", best_effort=True, text=event.text)
        elif isinstance(event, Reply):
            self._reply = event.text
        elif isinstance(event, TurnEnd) and event.reply:
            self._reply = event.reply

    def send(self, text: str) -> None:
        self._host.send(text)

    def state(self) -> str:
        return self._host.state()

    def screen(self, lines: int | None = None) -> str:
        return self._host.screen(lines)

    def stop(self) -> None:
        self._host.stop()

    def _refresh_metadata(self) -> None:
        if self._adapter is None or not self._info.native_id:
            return
        try:
            resolved = self._adapter.session_metadata(self._info)
            info = replace(self._info, transcript=resolved.transcript, resume=resolved.resume)
        except Exception:
            return
        if info != self._info:
            self._info = info
            records.note(
                self._channel,
                "session_metadata",
                best_effort=True,
                sid=self.sid,
                transcript=info.transcript,
                resume=info.resume,
            )

    def info(self) -> SessionInfo:
        self._refresh_metadata()
        return self._info

    @property
    def reply(self) -> str | None:
        return self._reply

    def usage(self) -> Usage | None:
        return self._driver.usage(self.runtime) if self._driver is not None else None


def _child_env(launch: Launch, cwd: str, drop: Collection[str] = ()) -> dict[str, str]:
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

    def __init__(
        self,
        run: Headless,
        runtime: str,
        spec: Spec,
        events,
        *,
        registry: registry.Registry,
        host=tmux_host,
    ):
        self.registry = registry
        _no_tui_keys(spec.name, events)
        self.run, self.runtime = run, runtime

    def launch(self, prompt: str, request: SessionRequest) -> Launch:
        return self.run.launch(prompt, request)

    def prepared(
        self, info: SessionInfo, workdir: str
    ) -> contextlib.AbstractContextManager[headless.HeadlessMode]:
        return contextlib.nullcontext(headless.HeadlessMode(self.run))

    def begin(self, host: headless.HeadlessMode) -> None:
        launch = self.command
        try:
            host.begin(launch.argv, cwd=self.cwd, env=_child_env(launch, self.cwd, self.drop))
        except (OSError, ValueError) as e:
            raise DriverError(f"cannot start {launch.argv[0]}: {e}") from e


class _TuiSteps(_LaunchSteps):
    @staticmethod
    def run_of(adapter: Adapter) -> Tui | None:
        return adapter.tui()

    def __init__(
        self,
        run: Tui,
        runtime: str,
        spec: Spec,
        events,
        *,
        registry: registry.Registry,
        host=tmux_host,
    ):
        self.registry = registry
        self.run, self.runtime, self.settings = run, runtime, registry.tui()
        self.host = host
        assert spec.sid is not None
        self.name = _name(f"{runtime}-{spec.sid[:8]}" if spec.name is None else spec.name)
        self.events = _events(events, host=self.host)
        self.tmux = self.host.tmux_path()
        if self.tmux is None:
            raise DriverError("tmux not found")

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
                self.events,
                runtime=self.runtime,
                workdir=workdir,
                tmux=self.tmux,
                host=self.host,
            )

    def begin(self, host: tui.TuiMode) -> None:
        launch = self.command
        host.begin(
            launch.argv,
            cwd=self.cwd,
            env=_child_env(launch, self.cwd, self.drop),
            status_line=self.settings.status_line,
        )


class _InProcessSteps:
    name = None
    specced = staticmethod(_no_tui_fields)

    @staticmethod
    def run_of(adapter: Adapter) -> InProcess | None:
        return adapter.in_process()

    def __init__(
        self,
        run: InProcess,
        runtime: str,
        spec: Spec,
        events,
        *,
        registry: registry.Registry,
        host=tmux_host,
    ):
        self.registry = registry
        _no_tui_keys(spec.name, events)
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
        self, info: SessionInfo, workdir: str
    ) -> contextlib.AbstractContextManager[in_process.InProcessMode]:
        return contextlib.nullcontext(in_process.InProcessMode(self.run))

    def begin(self, host: in_process.InProcessMode) -> None:
        host.begin(*self.args)


_STEPS = {"headless": _HeadlessSteps, "tui": _TuiSteps, "in_process": _InProcessSteps}


def _idle(idle_timeout) -> None:
    if idle_timeout is not None and (
        isinstance(idle_timeout, bool)
        or not isinstance(idle_timeout, (int, float))
        or not idle_timeout > 0
    ):
        raise ConfigError.idle_timeout(idle_timeout)


def _name(name) -> str:
    if not isinstance(name, str) or not tmux_host.NAME.fullmatch(name):
        raise ConfigError.session_name(name)
    return name


def _events(events, *, host=tmux_host) -> str | None:
    if events is None:
        return None
    if not isinstance(events, str) or not events.isprintable():
        raise ConfigError.events_path(events)
    try:
        return host.events_file(events)
    except tmux_host.TuiError as e:
        raise ConfigError(str(e)) from None


def _row(found: Mapping, own: str | None) -> SessionRow:
    sid, runtime, state = found["sid"], found["runtime"], found["state"]
    return SessionRow(
        name=found["name"],
        sid=None if sid_error(sid) else sid,
        runtime=runtime if isinstance(runtime, str) and NAME.match(runtime) else None,
        state="dead" if found["dead"] else state if state in ("done", "blocked") else "working",
        pane=found["pane"],
        opener=found["opener"],
        shown=found["shown"],
        last_lines=found["last_lines"],
        own=found["name"] == own,
    )


def _placement(split, split_from, opener=None) -> None:
    if split is not None and split not in tmux_host.SPLITS:
        raise ConfigError.split_direction(split)
    if split_from is not None and not (
        isinstance(split_from, str) and tmux_host.NAME.fullmatch(split_from)
    ):
        raise ConfigError.split_source(split_from)
    if opener is not None and not (isinstance(opener, str) and tmux_host.OPENER.fullmatch(opener)):
        raise ConfigError.opener(opener)


def _probe_dir() -> str | None:
    path = os.path.expanduser(PROBE_DIR)
    try:
        os.makedirs(path, 0o700, exist_ok=True)
        return path if stat.S_ISDIR(os.lstat(path).st_mode) else None
    except OSError:
        return None


def note(workdir: str, kind: str, **fields) -> None:
    """Appends a `kind` record to <workdir>/run.jsonl."""
    records.note(records.path(workdir), kind, **fields)


class Driver:
    """A caller owns the configuration for every session it starts or attaches."""

    def __init__(
        self,
        *,
        adapters: Mapping,
        hosts: Mapping,
        default_host: str,
        services: DriverServices | None = None,
    ):
        services = DriverServices() if services is None else services
        self.host, self.probe, self.clock = services.host, services.probe, services.clock
        self.registry = registry.Registry(adapters=adapters, hosts=hosts, default_host=default_host)

    @classmethod
    def from_config(cls, config: Mapping, *, services: DriverServices | None = None):
        try:
            return cls(**registry.settings(config), services=services)
        except ValueError as error:
            raise ConfigError(str(error)) from error

    def start(
        self,
        runtime: str,
        mode: str,
        prompt: str,
        spec: Spec,
        *,
        options: StartOptions | None = None,
    ) -> Handle:
        """spec.sid: its id (None: options.resume, else a new one); tui: tmux session spec.name,
        else <runtime>-<sid[:8]>, refused when running; options.events gets done, blocked and dead
        lines; spec.protect gains the hook paths.
        """
        options = StartOptions() if options is None else options
        resume, idle_timeout, events = options.resume, options.idle_timeout, options.events
        if mode not in MODES:
            raise ConfigError.mode(MODES, mode)
        _idle(idle_timeout)
        if resume is not None and not (isinstance(resume, str) and resume):
            raise ConfigError.resume(resume)
        if spec.sid is not None and resume is not None and spec.sid != resume:
            raise ConfigError.resume_mismatch(spec.sid, resume)
        adapter = self.registry.adapter(runtime)
        kind = _STEPS[mode]
        run = kind.run_of(adapter)
        if run is None:
            raise ConfigError.runtime_mode(runtime, mode)
        spec = replace(spec, sid=resume or spec.sid or str(uuid.uuid4()))
        steps = kind(run, runtime, spec, events, registry=self.registry, host=self.host)
        check_selection(spec.flags, adapter.forbidden_selection, adapter.valued_flags)
        spec = steps.specced(spec)
        if spec.access.commands and adapter.runtime_note:
            prompt = prompt + "\n\n" + adapter.runtime_note
        info = steps.info(prompt, SessionRequest(spec, resumed=resume is not None))
        with steps.prepared(info, spec.workdir) as host:
            os.makedirs(spec.workdir, exist_ok=True)
            records.note(
                records.path(spec.workdir),
                "session",
                sid=spec.sid,
                cwd=info.cwd,
                project=info.project,
                transcript=info.transcript,
                resume=info.resume,
            )
            steps.begin(host)
        return Handle(
            runtime,
            mode,
            replace(spec, name=steps.name),
            host=host,
            idle_timeout=idle_timeout,
            info=info,
            adapter=adapter,
            driver=self,
            clock=self.clock,
        )

    def attach(self, name: str, idle_timeout: float | None = None) -> Handle:
        """Tui session `name`'s Handle from any process: its reply the last turn's, its events from
        the next hook record.
        """
        _name(name)
        _idle(idle_timeout)
        state = tui.read_state(name)
        try:
            adapter = self.registry.adapter(state["runtime"])
            run = adapter.tui()
        except ConfigError as e:
            raise DriverError(f"no tui session {name}: {e}") from None
        if run is None:
            raise DriverError(
                f"no tui session {name}: runtime {state['runtime']!r} has no tui mode"
            )
        host = tui.TuiMode.attach(name, state, run, host=self.host)
        info = SessionInfo(
            state["sid"],
            host.bound,
            state["cwd"],
            state["project"],
            state["transcript"],
            state["resume"],
        )
        handle = Handle(
            state["runtime"],
            "tui",
            Spec(workdir=state["workdir"], sid=state["sid"], name=name),
            host=host,
            info=info,
            idle_timeout=idle_timeout,
            reply=host.reply,
            blocked=host.blocked,
            adapter=adapter,
            driver=self,
            clock=self.clock,
        )
        handle.info()
        return handle

    def show(
        self,
        name: str,
        split: str | None = None,
        split_from: str | None = None,
        opener: str | None = None,
    ) -> str | None:
        """Opens a pane on session `name` per the tui settings; None, else logs the reason and
        returns attach_command(name).
        """
        _name(name)
        _placement(split, split_from, opener)
        settings = self.registry.tui()
        if self.host.status(name) is None:
            raise DriverError(f"no tmux session {name}")
        why = self.host.open_pane(
            name,
            split=split,
            split_from=split_from,
            opener=opener,
            per_column=settings.per_column,
            retile=settings.retile,
        )
        if why is None:
            return None
        LOG.warning("show %s: %s", name, why)
        return self.host.attach_command(name)

    def usage(self, runtime: str) -> Usage | None:
        """None only when the probe fails: no probe, a failed start or timeout, or no reading in its
        output.
        """
        adapter = self.registry.adapter(runtime)
        launch = adapter.usage_probe()
        if launch is None or not launch.argv:
            return None
        cwd = launch.cwd or _probe_dir()
        if cwd is None:
            return None
        output = self.probe(
            launch.argv, cwd=cwd, env=_child_env(launch, cwd), timeout=PROBE_TIMEOUT
        )
        if output is None:
            return None
        try:
            return adapter.usage(output)
        except Exception:
            return None

    @staticmethod
    def session_record(workdir: str, sid: str) -> dict | None:
        return records.session_record(workdir, sid)

    def session_status(self, runtime: str, sid: str, cwd: str) -> SessionStatus:
        if error := sid_error(sid):
            raise ConfigError(error)
        if not printable_abs(cwd):
            raise ConfigError.cwd(cwd)
        adapter = self.registry.adapter(runtime)
        try:
            return adapter.session_status(sid, cwd)
        except OSError:
            return SessionStatus("unknown", None, "")

    def writable_dirs(self, runtime: str, spec: Spec, resume: str | None = None) -> tuple[str, ...]:
        """Resume: session `resume`'s. An OSError reading them is a ConfigError, so a caller fails
        closed.
        """
        if spec.sid is not None:
            raise ConfigError.writable_sid()
        if resume is not None and (error := sid_error(resume)):
            raise ConfigError(error)
        adapter = self.registry.adapter(runtime)
        try:
            return tuple(
                adapter.writable_dirs(
                    SessionRequest(replace(spec, sid=resume), resumed=resume is not None)
                )
            )
        except OSError as e:
            raise ConfigError.writable_directories(runtime, e) from e

    def sessions(self, match: str | None = None) -> list[SessionRow]:
        """Names re.fullmatching `match`, by name; DriverError, never [], when tmux's state is
        unknown.
        """
        if match is not None and not isinstance(match, str):
            raise ConfigError.match_type(match)
        try:
            pattern = None if match is None else re.compile(match)
        except re.error as e:
            raise ConfigError.invalid_match(match, e) from None
        found = self.host.listing(pattern)
        if found is None:
            raise DriverError.sessions_unavailable()
        try:
            own = self.host.own_session() if found else None
        except tmux_host.TuiError as e:
            raise DriverError.sessions_unavailable(e) from None
        return [_row(f, own) for f in found]

    def close(self, name: str) -> None:
        """Kills tui session `name` (absent is fine), then removes its session dir."""
        tui.close(_name(name), host=self.host)

    def layout(self, split: str | None = None, split_from: str | None = None) -> str | None:
        """show()'s opener once the pane it goes beside (split_from's, else the caller's) is found;
        None only with split_from.
        """
        _placement(split, split_from)
        if split_from is not None and self.host.status(split_from) is None:
            raise ConfigError.missing_tmux_session(split_from)
        try:
            self.host.anchor(split_from)
            try:
                return self.host.opener()
            except tmux_host.TuiError:
                if split_from is None:
                    raise
                return None
        except tmux_host.TuiError as e:
            hint = "" if split_from else ": run from tmux or iTerm2, or pass split_from"
            raise DriverError(f"no pane to show the TUI beside ({e}){hint}") from None

    def attach_command(self, name: str) -> str:
        return self.host.attach_command(_name(name))

    def detach(
        self,
        name: str,
        argv: Sequence[str],
        cwd: str,
        env: Mapping[str, str],
        *,
        opener: str | None = None,
    ) -> None:
        """Runs argv in new tmux session `name`, SIGNALS blocked until argv unblocks them; returns
        once argv has exec'd.
        """
        _name(name)
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or not all(isinstance(a, str) for a in argv)
        ):
            raise ConfigError.argv(argv)
        if not printable_abs(cwd):
            raise ConfigError.cwd(cwd)
        if not os.path.isdir(cwd):
            raise ConfigError.cwd_directory(cwd)
        _placement(None, None, opener)
        try:
            self.host.detach(name, list(argv), cwd=cwd, env=dict(env), block=SIGNALS, opener=opener)
        except tmux_host.TuiError as e:
            raise DriverError(f"detach {name}: {e}") from e
