"""HostedSession, the session layer's one concrete Session: an Adapter's runtime in a Mode on a
Host. events() yields its events, Stderr and IdleTimeout (none while Blocked) up to its Exited,
recording `stderr` and each ClientBound as `client_session`, never yielded.
"""

from __future__ import annotations

import builtins
import os
import re
import stat
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, cast

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.adapters.registry import Registry
from agent_pm.session.engine.base import Session
from agent_pm.session.engine.records import RunLog
from agent_pm.session.engine.types import (
    ActivityState,
    Blocked,
    ClientBound,
    ConfigError,
    DriverError,
    Exited,
    Hosts,
    IdleTimeout,
    Liveness,
    Mode,
    NotDisplayable,
    Reply,
    Resumable,
    SessionEvent,
    SessionInfo,
    SessionRow,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
    Word,
)
from agent_pm.session.hosts.base import NAME, Displayable, Host
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.tmux_host import TuiError
from agent_pm.session.modes import headless, in_process, tui
from agent_pm.session.modes.base import RunningMode, child_env
from agent_pm.utils import STATE, Display, Record, load_class, printable_abs, sid_error, thaw

POLL = 0.5
PROBE_DIR = f"{STATE}/probe"
PROBE_TIMEOUT = 120
UNCONFIGURED = "session not configured: call Session.configure first"
MODES: dict[Mode, type[RunningMode]] = {
    mode.kind: mode for mode in (headless.HeadlessMode, tui.TuiMode, in_process.InProcessMode)
}

HostFactory = Callable[[type[Host], str, Mapping], Host]


def _built(cls: type[Host], name: str, settings: Mapping) -> Host:
    return cls(name, settings)


@dataclass(frozen=True)
class _Setup:
    registry: Registry
    hosts: Hosts
    class_loader: Callable[..., Any]
    tmux: Any
    probe: Callable[..., str | None]
    clock: Callable[[], float]
    host_factory: HostFactory
    built: dict[str, Host] = field(default_factory=dict, compare=False)


def _idle(idle_timeout) -> None:
    if idle_timeout is not None and (
        isinstance(idle_timeout, bool)
        or not isinstance(idle_timeout, (int, float))
        or not idle_timeout > 0
    ):
        raise ConfigError(
            f"idle_timeout must be a positive number of seconds, got {idle_timeout!r}"
        )


def _displayed(display) -> None:
    if why := tmux_host.display_error(display):
        raise ConfigError(why)


def _session_line(workdir: str) -> Callable[[SessionInfo], None]:
    def record(info: SessionInfo) -> None:
        os.makedirs(workdir, exist_ok=True)
        RunLog(workdir).note("session", **info.persisted())

    return record


def _activity(found: Mapping) -> ActivityState:
    if found["dead"]:
        return ActivityState.DEAD
    state = found["state"]
    return ActivityState(state) if state in (Word.DONE, Word.BLOCKED) else ActivityState.WORKING


def _row(found: Mapping, own: str | None) -> SessionRow:
    sid, runtime = found["sid"], found["runtime"]
    return SessionRow(
        name=found["name"],
        sid=None if sid_error(sid) else sid,
        runtime=runtime if isinstance(runtime, str) and NAME.match(runtime) else None,
        state=_activity(found),
        pane=found["pane"],
        opener=found["opener"],
        shown=found["shown"],
        last_lines=found["last_lines"],
        own=found["name"] == own,
    )


def _resumable(adapter: Callable[[], Adapter], sid: str, cwd: str) -> SessionStatus:
    if error := sid_error(sid):
        raise ConfigError(error)
    if not printable_abs(cwd):
        raise ConfigError(f"cwd must be a printable absolute path, got {cwd!r:.80}")
    try:
        return adapter().session_status(sid, cwd)
    except OSError:
        return SessionStatus(Resumable.UNKNOWN, None, "")


def _probe_dir() -> str | None:
    path = os.path.expanduser(PROBE_DIR)
    try:
        os.makedirs(path, 0o700, exist_ok=True)
        return path if stat.S_ISDIR(os.lstat(path).st_mode) else None
    except OSError:
        return None


class HostedSession(Session):
    """Each configured class holds its own Setup; a subclass starts unconfigured."""

    _setup: ClassVar[_Setup | None] = None

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        cls._setup = None

    def __init__(
        self,
        runtime: str,
        mode: Mode,
        spec: Spec,
        *,
        info: SessionInfo,
        running: RunningMode,
        host: Host,
        idle_timeout: float | None = None,
        adapter: Adapter,
    ):
        self.sid, self.runtime, self.mode, self.name = info.sid, runtime, mode, spec.name
        self._running, self._host, self._info, self._idle = running, host, info, idle_timeout
        self._adapter = adapter
        self._clock = type(self)._config().clock
        self._log = RunLog(spec.workdir)
        self._reply: str | None = None
        self._queue: deque = deque()
        self._closed = False
        self._ended = False
        self._blocked = False
        self._bound = False

    @classmethod
    def configure(
        cls,
        *,
        adapters: Mapping,
        hosts: Hosts,
        class_loader=load_class,
        tmux=tmux_host,
        probe=headless.probe,
        clock: Callable[[], float] = time.monotonic,
        host_factory: HostFactory | None = None,
    ) -> None:
        """tmux: the module its Modes and list() use; host_factory(cls, name, settings) -> Host."""
        if not isinstance(hosts, Hosts):
            raise ConfigError("hosts: want parsed Hosts (Host.parse_hosts)")
        registry = Registry(adapters=adapters, hosts=hosts, class_loader=class_loader)
        cls._setup = _Setup(
            registry, hosts, class_loader, tmux, probe, clock, host_factory or _built
        )

    @classmethod
    def configured(cls, **setup) -> type[HostedSession]:
        own = cast("type[HostedSession]", type(cls.__name__, (cls,), {"__module__": __name__}))
        own.configure(**setup)
        return own

    @classmethod
    def _rebound(cls, **collaborators) -> type[HostedSession]:
        own = cast("type[HostedSession]", type(cls.__name__, (cls,), {"__module__": __name__}))
        own._setup = replace(cls._config(), built={}, **collaborators)
        return own

    @classmethod
    def _config(cls) -> _Setup:
        if cls._setup is None:
            raise ConfigError(UNCONFIGURED)
        return cls._setup

    @classmethod
    def _tmux(cls):
        return tmux_host if cls._setup is None else cls._setup.tmux

    @classmethod
    def start(
        cls,
        spec: Spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> HostedSession:
        """spec.sid: its id, else a new one; display: kept by its Host."""
        setup = cls._config()
        _idle(idle_timeout)
        _displayed(display)
        if not isinstance(spec, Spec):
            raise ConfigError(f"spec: want a Spec, got {type(spec).__name__}")
        runtime, mode = spec.runtime, spec.mode
        if runtime is None or mode is None:
            raise ConfigError(f"spec.{'runtime' if runtime is None else 'mode'}: required to start")
        on = cls.host(host)
        adapter = setup.registry.adapter(runtime)
        spec = replace(spec, sid=spec.sid or str(uuid.uuid4()))
        running = MODES[mode].start(
            spec,
            adapter,
            registry=setup.registry,
            record=_session_line(spec.workdir),
            host=setup.tmux,
            display=display,
        )
        return cls(
            runtime,
            mode,
            running.prepared.spec,
            info=running.prepared.info,
            running=running,
            host=on,
            idle_timeout=idle_timeout,
            adapter=adapter,
        )

    @classmethod
    def attach(
        cls, name: str, *, host: str | None = None, idle_timeout: float | None = None
    ) -> HostedSession:
        """Tui session `name`, from any process; its hook log replayed into its reply (see reply)
        and blocked, blocked too while its pane is marked so.
        """
        setup = cls._config()
        tui.checked_name(name)
        _idle(idle_timeout)
        on = cls.host(host)
        state = tui.TuiMode.snapshot(name)
        try:
            adapter = setup.registry.adapter(state["runtime"])
        except ConfigError as e:
            raise DriverError(f"no tui session {name}: {e}") from None
        running = tui.TuiMode.attach(name, state, adapter, host=setup.tmux)
        session = cls(
            state["runtime"],
            Mode.TUI,
            running.prepared.spec,
            info=running.prepared.info,
            running=running,
            host=on,
            idle_timeout=idle_timeout,
            adapter=adapter,
        )
        for event in running.replayed:
            session._fold(event, replayed=True)
        if not session._blocked:
            session._blocked = running.marked_blocked()
        session.info()
        return session

    @classmethod
    def list(
        cls, match: str | None = None, *, display: Display = None
    ) -> builtins.list[SessionRow]:
        """Tmux sessions fullmatching `match`, stored with `display` if given ("": none)."""
        if match is not None and not isinstance(match, str):
            raise ConfigError(f"match must be a regular expression, got {match!r:.80}")
        try:
            pattern = None if match is None else re.compile(match)
        except re.error as e:
            raise ConfigError(f"match {match!r:.80}: {e}") from None
        if display == "":
            return []
        tmux = cls._tmux()
        found = tmux.listing(pattern)
        if found is None:
            raise DriverError("tmux sessions unknown: tmux failed")
        if display is not None:
            found = [f for f in found if f.get("display") == display]
        try:
            own = tmux.own_session() if found else None
        except TuiError as e:
            raise DriverError(f"tmux sessions unknown: {e}") from None
        return [_row(f, own) for f in found]

    @classmethod
    def usage(cls, runtime: str) -> Usage | None:
        setup = cls._config()
        adapter = setup.registry.adapter(runtime)
        launch = adapter.usage_probe()
        if launch is None or not launch.argv:
            return None
        cwd = launch.cwd or _probe_dir()
        if cwd is None:
            return None
        output = setup.probe(
            launch.argv, cwd=cwd, env=child_env(launch, cwd), timeout=PROBE_TIMEOUT
        )
        if output is None:
            return None
        try:
            return adapter.usage(output)
        except ValueError:
            return None

    @classmethod
    def layout(cls, *, display: Display = None) -> str | None:
        on = cls.host()
        _displayed(display)
        return on.layout(display=display) if isinstance(on, Displayable) else None

    @classmethod
    def writable_dirs(cls, spec: Spec) -> tuple[str, ...]:
        """An OSError is a ConfigError, so a caller fails closed."""
        setup = cls._config()
        if not isinstance(spec, Spec):
            raise ConfigError(f"spec: want a Spec, got {type(spec).__name__}")
        runtime = spec.runtime
        if runtime is None:
            raise ConfigError("spec.runtime: required")
        adapter = setup.registry.adapter(runtime)
        try:
            return tuple(adapter.writable_dirs(spec))
        except OSError as e:
            raise ConfigError(f"{runtime}'s writable dirs: {e}") from e

    @classmethod
    def display(cls, value: Display = None, *, host: str | None = None) -> Display:
        on = cls.host(host)
        _displayed(value)
        return on.display(value)

    @classmethod
    def host(cls, name: str | None = None) -> Host:
        """[hosts.<name>] (None: the default), built once per class."""
        setup = cls._config()
        name = setup.hosts.default if name is None else name
        if not isinstance(name, str) or name not in setup.hosts.tables:
            raise ConfigError(f"unknown host {name}")
        found = setup.built.get(name)
        if found is None:
            settings = thaw(setup.hosts.tables[name])
            path = settings.pop("implementation", None)
            try:
                kind = setup.class_loader(path, Host)
            except ValueError as e:
                raise ConfigError(f"[hosts.{name}].implementation: {e}") from None
            found = setup.built.setdefault(name, setup.host_factory(kind, name, settings))
        return found

    @classmethod
    def inspect(cls, spec: Spec) -> SessionStatus:
        setup = cls._config()
        if not isinstance(spec, Spec):
            raise ConfigError(f"spec: want a Spec, got {type(spec).__name__}")
        runtime, sid = spec.runtime, spec.sid
        if runtime is None or sid is None:
            field = "runtime" if runtime is None else "sid"
            raise ConfigError(f"spec.{field}: required to inspect")
        try:
            info = RunLog(spec.workdir).session(sid)
        except OSError as error:
            raise DriverError(f"inspect session {sid}: {error}") from error
        if info is None:
            raise DriverError(f"no recorded session {sid} in {spec.workdir}")
        return _resumable(lambda: setup.registry.adapter(runtime), sid, info.cwd)

    def events(self) -> Iterator[SessionEvent | Stderr | IdleTimeout]:
        last = self._clock()
        try:
            while not self._ended:
                if self._queue:
                    event = self._queue.popleft()
                    self._record(event)
                    if not isinstance(event, ClientBound):
                        last = self._clock()
                        yield event
                    continue
                wait = POLL
                if self._idle is not None and not self._blocked:
                    wait = min(POLL, last + self._idle - self._clock())
                if self._closed:
                    code = self._running.returncode(max(wait, 0))
                    if code is not None:
                        self._ended = True
                        yield Exited(code)
                        continue
                elif wait > 0:
                    found, self._closed = self._running.poll(wait)
                    self._queue.extend(e for e in found if not isinstance(e, Exited))
                    continue
                if wait <= 0 and self._idle is not None:
                    last = self._clock()
                    yield IdleTimeout(self._idle)
        except GeneratorExit:  # the caller closed this iterator; the session goes on
            raise
        except BaseException:
            self._ended = True
            self._running.stop()
            raise

    def _record(self, event: SessionEvent | Stderr | ClientBound) -> None:
        """A live event: its client_session or stderr line, then its _fold."""
        if isinstance(event, ClientBound):
            self._log.note(
                "client_session",
                sid=self.sid,
                client_id=event.client_id,
                durable=True,
            )
        elif isinstance(event, Stderr):
            self._log.note("stderr", best_effort=True, text=event.text)
        self._fold(event)
        if isinstance(event, ClientBound):
            self._refresh_metadata()

    def _fold(self, event: SessionEvent | Stderr | ClientBound, *, replayed: bool = False) -> None:
        """What an event, live or replayed on attach, changes: bound, reply, blocked."""
        if isinstance(event, ClientBound):
            self._bound = True
            return
        if isinstance(event, Reply):
            self._reply = event.text
        elif isinstance(event, TurnEnd) and (event.reply or replayed):
            self._reply = event.reply
        self._blocked = isinstance(event, Blocked) or self._blocked and isinstance(event, Stderr)

    def send(self, text: str) -> None:
        self._running.send(text)

    def state(self) -> Liveness:
        return self._running.state()

    def stop(self) -> None:
        self._running.stop()

    def _refresh_metadata(self) -> None:
        if not self._bound:
            return
        try:
            resolved = self._adapter.session_metadata(self._info)
            info = replace(self._info, transcript=resolved.transcript, resume=resolved.resume)
        except OSError:
            return
        if info != self._info:
            self._info = info
            self._log.note(
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
        """The latest Reply or non-empty TurnEnd text, None before; attached, its hook log's last
        TurnEnd reply, even "", comes first.
        """
        return self._reply

    def close(self) -> None:
        """tui: as the close(name) wrapper (FR-4), on the tmux it started on; else stop()."""
        running = self._running
        if isinstance(running, tui.TuiMode):
            tui.TuiMode.close(running.name, host=running.host)
        else:
            self.stop()

    def session_status(self) -> SessionStatus:
        return _resumable(lambda: self._adapter, self.sid, self._info.cwd)

    def record(self) -> Record | None:
        name = self._tui()
        return None if name is None else self._host.attach(name).record()

    @property
    def displayable(self) -> bool:
        return self._tui() is not None and isinstance(self._host, Displayable)

    def show(self, *, display: Display = None) -> str | None:
        host, name = self._shown("show")
        if display is None:
            display = self._host.attach(name).display()
        return host.show(name, display=display)

    def attach_command(self) -> str:
        host, name = self._shown("attach_command")
        return host.attach_command(name)

    def screen(self, lines: int | None = None) -> str:
        """Its mode's read, which refuses a pane no longer running."""
        self._shown("screen")
        return self._running.screen(lines)

    def _tui(self) -> str | None:
        return self.name if self.mode == Mode.TUI else None

    def _shown(self, call: str) -> tuple[Displayable, str]:
        if not isinstance(self._host, Displayable):
            raise NotDisplayable(f"host {self._host.name} cannot display")
        name = self._tui()
        if name is None:
            raise NotDisplayable(f"{call}: tui mode only")
        return self._host, name
