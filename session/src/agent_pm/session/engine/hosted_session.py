"""HostedSession, the session layer's one concrete Session: an Adapter's client in a Mode on a
Host. events() yields its events, Stderr and IdleTimeout (none while Blocked) up to its Exited,
recording `stderr` and each ClientBound as `client_session`, never yielded.
"""

from __future__ import annotations

import builtins
import os
import re
import stat
import sys
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, cast

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.adapters.registry import Registry, settings
from agent_pm.session.engine.base import Session
from agent_pm.session.engine.records import RunLog
from agent_pm.session.engine.types import (
    Blocked,
    ClientBound,
    ConfigError,
    Exited,
    Hosts,
    IdleTimeout,
    KeyPress,
    Liveness,
    Mode,
    NoRecordedSession,
    NotDisplayable,
    Reply,
    Resumable,
    SessionError,
    SessionEvent,
    SessionInfo,
    SessionRow,
    SessionStatus,
    Spec,
    Stderr,
    TurnEnd,
    Usage,
)
from agent_pm.session.hosts.base import Displayable, Host
from agent_pm.session.modes import headless, in_process, tui
from agent_pm.session.modes.base import Runtime, child_env
from agent_pm.utils import (
    STATE,
    Display,
    Record,
    load_class,
    module_roots,
    printable_abs,
    probe,
    sid_error,
    thaw,
)


def _request(spec: Spec, idle_timeout: float | None) -> tuple[str, Mode]:
    _idle(idle_timeout)
    if not isinstance(spec, Spec):
        raise ConfigError(f"spec: want a Spec, got {type(spec).__name__}")
    client, mode = spec.client, spec.mode
    if client is None or mode is None:
        raise ConfigError(f"spec.{'client' if client is None else 'mode'}: required to start")
    return client, mode


POLL = 0.5
PROBE_DIR = f"{STATE}/probe"
PROBE_TIMEOUT = 120
UNCONFIGURED = "session not configured: call Session.configure first"
MODES: dict[Mode, type[Runtime]] = {
    mode.kind: mode
    for mode in (headless.HeadlessRuntime, tui.TuiRuntime, in_process.InProcessRuntime)
}

HostFactory = Callable[[type[Host], str, Mapping], Host]


def _built(cls: type[Host], name: str, settings: Mapping) -> Host:
    return cls(name, settings)


@dataclass(frozen=True)
class _Setup:
    registry: Registry
    hosts: Hosts
    class_loader: Callable[..., Any]
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


def _session_line(workdir: str) -> Callable[[SessionInfo], None]:
    def record(info: SessionInfo) -> None:
        os.makedirs(workdir, exist_ok=True)
        RunLog(workdir).note("session", **info.persisted())

    return record


def _resumable(adapter: Callable[[], Adapter], info: SessionInfo, spec: Spec) -> SessionStatus:
    if error := sid_error(info.sid):
        raise ConfigError(error)
    if not printable_abs(info.cwd):
        raise ConfigError(f"cwd must be a printable absolute path, got {info.cwd!r:.80}")
    try:
        return adapter().session_status(info, env={**spec.access.env, **spec.env})
    except OSError:
        return SessionStatus(Resumable.UNKNOWN, None, "")


def _probe_dir() -> str | None:
    path = os.path.expanduser(PROBE_DIR)
    try:
        os.makedirs(path, 0o700, exist_ok=True)
        return path if stat.S_ISDIR(os.lstat(path).st_mode) else None
    except OSError:
        return None


def _host_class(setup: _Setup, name: str) -> tuple[type[Host], dict]:
    """[hosts.<name>]'s class and its other settings."""
    settings = thaw(setup.hosts.tables[name])
    path = settings.pop("implementation", None)
    try:
        return setup.class_loader(path, Host), settings
    except ValueError as e:
        raise ConfigError(f"[hosts.{name}].implementation: {e}") from None


def _class_root(kind: type, table: str) -> str:
    module = sys.modules.get(kind.__module__)
    if module is None or getattr(module, "__file__", None) is None:
        raise ConfigError(f"{table}.implementation: {kind.__module__} has no source file")
    return module_roots(module)[0]


class HostedSession(Session):
    """Each configured class holds its own Setup; a subclass starts unconfigured."""

    _setup: ClassVar[_Setup | None] = None

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        cls._setup = None

    def __init__(
        self,
        client: str,
        mode: Mode,
        spec: Spec,
        *,
        info: SessionInfo,
        running: Runtime,
        host: Host,
        idle_timeout: float | None = None,
        adapter: Adapter,
    ):
        self.sid, self.client, self.mode, self.name = info.sid, client, mode, spec.name
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
        probe=probe,
        clock: Callable[[], float] = time.monotonic,
        host_factory: HostFactory | None = None,
    ) -> None:
        """host_factory(cls, name, settings) builds a configured Host."""
        if not isinstance(hosts, Hosts):
            raise ConfigError("hosts: want parsed Hosts (Host.parse_hosts)")
        registry = Registry(adapters=adapters, hosts=hosts, class_loader=class_loader)
        cls._setup = _Setup(registry, hosts, class_loader, probe, clock, host_factory or _built)

    @classmethod
    def configure_from(cls, config: Mapping, *, hosts: Hosts | None = None) -> None:
        cls.configure(**settings(config, hosts=hosts))

    @classmethod
    def configured(cls, **setup) -> type[HostedSession]:
        own = cast("type[HostedSession]", type(cls.__name__, (cls,), {"__module__": __name__}))
        own.configure(**setup)
        return own

    @classmethod
    def _config(cls) -> _Setup:
        if cls._setup is None:
            raise ConfigError(UNCONFIGURED)
        return cls._setup

    @classmethod
    def preflight(
        cls,
        spec: Spec,
        *,
        display: Display = None,
        host: str | None = None,
        idle_timeout: float | None = None,
    ) -> None:
        setup = cls._config()
        client, mode = _request(spec, idle_timeout)
        adapter = setup.registry.adapter(client)
        spec = replace(spec, sid=spec.sid or str(uuid.uuid4()))
        on = cls.host(host)
        on.validate_display(display)
        MODES[mode].preflight(
            spec,
            adapter,
            registry=setup.registry,
            host=on,
            display=display,
        )

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
        client, mode = _request(spec, idle_timeout)
        adapter = setup.registry.adapter(client)
        spec = replace(spec, sid=spec.sid or str(uuid.uuid4()))
        on = cls.host(host)
        on.validate_display(display)
        name = tui.TuiRuntime.resolve_name(spec) if mode == Mode.TUI else spec.name

        def check_name(name: str) -> None:
            if any(
                name in cls.host(configured).list()
                for configured in setup.hosts.tables
                if cls.host(configured) is not on
            ):
                raise SessionError(f"session {name} exists")

        if name is not None:
            check_name(name)
        running = MODES[mode].start(
            spec,
            adapter,
            registry=setup.registry,
            record=_session_line(spec.workdir),
            host=on,
            display=display,
            check_name=check_name,
        )
        return cls(
            client,
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
        """Tui session `name`, from any process; its client record and hook log replayed into its
        reply and blocked, blocked too while its terminal is marked so.
        """
        setup = cls._config()
        tui.checked_name(name)
        _idle(idle_timeout)
        on = cls.host(host)
        state = tui.TuiRuntime.snapshot(name)
        try:
            adapter = setup.registry.adapter(state["client"])
        except ConfigError as e:
            raise SessionError(f"no tui session {name}: {e}") from None
        running = tui.TuiRuntime.attach(name, state, adapter, host=on)
        session = cls(
            state["client"],
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
    def retire(cls, name: str, *, host: str | None = None) -> None:
        tui.TuiRuntime.close(tui.checked_name(name), host=cls.host(host))

    @classmethod
    def list(
        cls, match: str | None = None, *, display: Display = None
    ) -> builtins.list[SessionRow]:
        """All configured Hosts' sessions fullmatching `match`, filtered by their displays."""
        if match is not None and not isinstance(match, str):
            raise ConfigError(f"match must be a regular expression, got {match!r:.80}")
        try:
            pattern = None if match is None else re.compile(match)
        except re.error as error:
            raise ConfigError(f"match {match!r:.80}: {error}") from None
        setup = cls._config()
        if display == "":
            return []
        return sorted(
            (
                row
                for name in setup.hosts.tables
                for row in cls.host(name).rows(display)
                if pattern is None or pattern.fullmatch(row.name)
            ),
            key=lambda row: row.name,
        )

    @classmethod
    def client_resume(cls, client: str, argv: Sequence[str]) -> int:
        """Runs `client`'s SessionInfo.resume command."""
        return cls._config().registry.adapter(client).resume_main(argv)

    @classmethod
    def usage(cls, client: str) -> Usage | None:
        setup = cls._config()
        adapter = setup.registry.adapter(client)
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
        on.validate_display(display)
        return on.layout(display=display) if isinstance(on, Displayable) else None

    @classmethod
    def writable_dirs(cls, spec: Spec) -> tuple[str, ...]:
        """An OSError is a ConfigError, so a caller fails closed."""
        setup = cls._config()
        if not isinstance(spec, Spec):
            raise ConfigError(f"spec: want a Spec, got {type(spec).__name__}")
        client = spec.client
        if client is None:
            raise ConfigError("spec.client: required")
        adapter = setup.registry.adapter(client)
        try:
            return tuple(adapter.writable_dirs(spec))
        except OSError as e:
            raise ConfigError(f"{client}'s writable dirs: {e}") from e

    @classmethod
    def display(cls, value: Display = None, *, host: str | None = None) -> Display:
        on = cls.host(host)
        on.validate_display(value)
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
            kind, settings = _host_class(setup, name)
            found = setup.built.setdefault(name, setup.host_factory(kind, name, settings))
        return found

    @classmethod
    def class_roots(cls, client: str) -> tuple[str, ...]:
        """Loads each class without building it; an unloadable one is a ConfigError."""
        setup = cls._config()
        loaded: list[tuple[str, type]] = [
            (f"[adapters.{client}]", setup.registry.adapter_class(client))
        ]
        loaded += [(f"[hosts.{name}]", _host_class(setup, name)[0]) for name in setup.hosts.tables]
        return tuple(dict.fromkeys(_class_root(kind, table) for table, kind in loaded))

    @classmethod
    def inspect(cls, spec: Spec) -> SessionStatus:
        setup = cls._config()
        if not isinstance(spec, Spec):
            raise ConfigError(f"spec: want a Spec, got {type(spec).__name__}")
        client, sid = spec.client, spec.sid
        if client is None or sid is None:
            field = "client" if client is None else "sid"
            raise ConfigError(f"spec.{field}: required to inspect")
        try:
            info = RunLog(spec.workdir).session(sid)
        except OSError as error:
            raise SessionError(f"inspect session {sid}: {error}") from error
        if info is None:
            raise NoRecordedSession(f"no recorded session {sid} in {spec.workdir}")
        return _resumable(lambda: setup.registry.adapter(client), info, spec)

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
        # a client record's Reply can land after the block it preceded
        self._blocked = (
            isinstance(event, Blocked) or self._blocked and isinstance(event, (Stderr, Reply))
        )

    def send(self, text: str) -> None:
        self._running.send(text)

    def press(self, keys: Sequence[KeyPress]) -> None:
        self._running.press(tuple(keys))

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
        TurnEnd reply, even "", comes first, else its client record's newest text.
        """
        return self._reply

    def close(self) -> None:
        """Close its owned client and retire its state."""
        running = self._running
        if isinstance(running, tui.TuiRuntime):
            tui.TuiRuntime.close(running.name, host=running.host)
        else:
            self.stop()

    def session_status(self) -> SessionStatus:
        return _resumable(lambda: self._adapter, self.info(), self._running.prepared.spec)

    def record(self) -> Record | None:
        name = self._tui()
        return None if name is None else self._host.attach(name).record()

    @property
    def displayable(self) -> bool:
        return self._tui() is not None and isinstance(self._host, Displayable)

    def show(self, *, display: Display = None, command: str | None = None) -> str | None:
        host, name = self._shown("show")
        if display is None:
            display = self._host.attach(name).display()
        return host.show(name, display=display, command=command)

    def attach_command(self) -> str:
        host, name = self._shown("attach_command")
        return host.attach_command(name)

    def screen(self, lines: int | None = None) -> str:
        """Its mode's read, which refuses a terminal no longer running."""
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
