"""The Driver's API. start() checks everything, writes a session's `session` record to <workdir>/run.jsonl, then
starts it in its Mode (headless, tui or in_process) and returns its Handle. Handle.events() streams its events, Stderr
and IdleTimeout (none while Blocked; it stops nothing) up to the Driver's own Exited, recording `client_session` and
`stderr`. usage(), session_info() and session() give a runtime's Usage, a session's SessionStatus and its record.
attach(), sessions(), show() and close() reach a tui session by name; detach() runs a command in a tmux session of its
own."""
import contextlib
import logging
import os
import signal
import stat
import time
import uuid
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from adapters.base import (RESUMABLE, Access, Activity, Adapter, Blocked, ConfigError, Event, Exited, Headless,
                           InProcess, Launch, LimitHit, Reply, SessionBound, SessionInfo, SessionStatus, Spec, Stderr,
                           Tui, TurnEnd, Usage, Window, sid_error)
from driver import records, registry, tmux_host
from driver.errors import DriverError
from driver.modes import headless, in_process, tui
from driver.records import session
from utils.paths import printable_abs

__all__ = ["Access", "Activity", "Blocked", "ConfigError", "DriverError", "Event", "Exited", "Handle", "IdleTimeout",
           "LimitHit", "MODES", "Mode", "POLL", "RESUMABLE", "Reply", "SIGNALS", "STATES", "SessionBound",
           "SessionInfo", "SessionStatus", "Sessions", "Spec", "Stderr", "TurnEnd", "Usage", "Window", "attach",
           "attach_command", "close", "detach", "layout", "records", "session", "session_info", "sessions", "show",
           "start", "usage"]

MODES = ("headless", "tui", "in_process")
STATES = ("live", "absent", "unknown")
POLL = 0.5
PROBE_DIR = "~/.agent-pm/probe"
PROBE_TIMEOUT = 120
SIGNALS = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
LOG = logging.getLogger("driver")


@dataclass(frozen=True)
class IdleTimeout:
    seconds: float


@dataclass(frozen=True)
class Sessions:
    """The tmux sessions named `prefix`…; names None: tmux's state is unknown."""
    prefix: str
    names: tuple[str, ...] | None

    def state(self, name: str) -> str:
        """One of STATES."""
        if not isinstance(name, str) or not name.startswith(self.prefix):
            raise ValueError(f"{name!r:.80} does not start with {self.prefix!r}")
        return "unknown" if self.names is None else "live" if name in self.names else "absent"


class Mode(Protocol):
    """A running session as one mode hosts it; a Handle's only way to it. Its own failure raises from poll (headless:
    its parse); the session's is its returncode (an in_process run that raised: 1)."""

    def poll(self, timeout: float) -> tuple[list[Event | Stderr], bool]:
        """The events that came, waiting up to `timeout` s for one, and whether no more will come."""

    def returncode(self, timeout: float) -> int | None:
        """The exit status, waiting at most `timeout` s; None while it runs; negative: killed by that signal."""

    def state(self) -> str:
        """One of STATES."""

    def send(self, text: str) -> None:
        """Submits `text` to the session."""

    def screen(self, lines: int | None = None) -> str:
        """The session's visible text."""

    def stop(self) -> None:
        """Ends the session; idempotent."""


class Handle:

    def __init__(self, sid: str, runtime: str, mode: str, *, host: Mode, workdir: str, info: SessionInfo,
                 idle_timeout: float | None, name: str | None = None, reply: str | None = None):
        self.sid, self.runtime, self.mode, self.name = sid, runtime, mode, name
        self._host, self._info, self._idle = host, info, idle_timeout
        self._channel = records.path(workdir)
        self._reply = reply
        self._queue: deque = deque()
        self._closed = False
        self._ended = False
        self._blocked = False

    def events(self) -> Iterator[Event | Stderr | IdleTimeout]:
        last = time.monotonic()
        try:
            while not self._ended:
                if self._queue:
                    event = self._queue.popleft()
                    self._record(event)
                    last = time.monotonic()
                    self._blocked = isinstance(event, Blocked) or self._blocked and isinstance(event, Stderr)
                    yield event
                    continue
                wait = POLL
                if self._idle is not None and not self._blocked:
                    wait = min(POLL, last + self._idle - time.monotonic())
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
                if wait <= 0:
                    last = time.monotonic()
                    yield IdleTimeout(self._idle)
        except GeneratorExit:   # the caller closed this iterator; the session goes on
            raise
        except BaseException:
            self._ended = True
            self._host.stop()
            raise

    def _record(self, event: Event | Stderr) -> None:
        if isinstance(event, SessionBound):
            records.note(self._channel, "client_session", sid=self.sid, client_id=event.native_id, durable=True)
            self._info = replace(self._info, native_id=event.native_id)
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

    def info(self) -> SessionInfo:
        return self._info

    @property
    def reply(self) -> str | None:
        return self._reply

    def usage(self) -> Usage | None:
        return usage(self.runtime)


def _child_env(launch: Launch, cwd: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in launch.unset}
    return {**env, **launch.env, "PWD": cwd}


def start(runtime: str, mode: str, prompt: str, spec: Spec, resume: str | None = None,
          idle_timeout: float | None = None, name: str | None = None, sid: str | None = None,
          events: str | None = None) -> Handle:
    """tui: tmux session `name`, else <runtime>-<sid[:8]>, refused when running; `events` gets its done, blocked and
    dead lines."""
    if mode not in MODES:
        raise ConfigError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
    _idle(idle_timeout)
    if resume is not None and not (isinstance(resume, str) and resume):
        raise ConfigError(f"resume must be a session id, got {resume!r:.80}")
    if sid is not None and (error := sid_error(sid)):
        raise ConfigError(error)
    if sid is not None and resume is not None and sid != resume:
        raise ConfigError(f"sid {sid!r:.80} differs from resume {resume!r:.80}")
    if spec.sid is not None:
        raise ConfigError("spec.sid is set by start(): pass sid= or resume=")
    adapter = registry.adapter(runtime)
    kind = _STEPS[mode]
    run = kind.run_of(adapter)
    if run is None:
        raise ConfigError(f"runtime {runtime!r} does not support {mode} mode")
    sid = resume or sid or str(uuid.uuid4())
    steps = kind(run, runtime, sid, name, events)
    spec = replace(spec, sid=sid)
    if spec.access.commands and adapter.runtime_note:
        prompt = prompt + "\n\n" + adapter.runtime_note
    info = steps.info(prompt, spec, resume is not None)
    with steps.prepared(info, spec.workdir) as host:
        os.makedirs(spec.workdir, exist_ok=True)
        records.note(records.path(spec.workdir), "session", sid=sid, cwd=info.cwd, project=info.project,
                     transcript=info.transcript, resume=info.resume)
        steps.begin(host)
    return Handle(sid, runtime, mode, host=host, workdir=spec.workdir, idle_timeout=idle_timeout, info=info,
                  name=steps.name)


def _no_tui_keys(name, events) -> None:
    for key, value in (("name", name), ("events", events)):
        if value is not None:
            raise ConfigError(f"{key}: tui mode only")


class _LaunchSteps:
    """A Mode starting the command its Adapter's launch gives; info() keeps that Launch and its cwd for begin()."""

    def info(self, prompt: str, spec: Spec, resumed: bool) -> SessionInfo:
        self.command = launch = self.launch(prompt, spec, resumed)
        if not launch.argv:
            raise ConfigError(f"runtime {self.runtime!r} gave no command")
        self.cwd = launch.cwd or spec.access.cwd or spec.workdir
        return SessionInfo(spec.sid, "", self.cwd, spec.access.project, launch.transcript, launch.resume)


class _HeadlessSteps(_LaunchSteps):
    name = None

    @staticmethod
    def run_of(adapter: Adapter) -> Headless | None:
        return adapter.headless()

    def __init__(self, run: Headless, runtime: str, sid: str, name, events):
        _no_tui_keys(name, events)
        self.run, self.runtime = run, runtime

    def launch(self, prompt: str, spec: Spec, resumed: bool) -> Launch:
        return self.run.launch(prompt, spec, resumed)

    def prepared(self, info: SessionInfo, workdir: str) -> contextlib.AbstractContextManager[headless.HeadlessMode]:
        return contextlib.nullcontext(headless.HeadlessMode(self.run))

    def begin(self, host: headless.HeadlessMode) -> None:
        launch = self.command
        try:
            host.begin(launch.argv, cwd=self.cwd, env=_child_env(launch, self.cwd))
        except (OSError, ValueError) as e:
            raise DriverError(f"cannot start {launch.argv[0]}: {e}") from e


class _TuiSteps(_LaunchSteps):
    @staticmethod
    def run_of(adapter: Adapter) -> Tui | None:
        return adapter.tui()

    def __init__(self, run: Tui, runtime: str, sid: str, name, events):
        self.run, self.runtime, self.settings = run, runtime, registry.tui()
        self.name = _name(f"{runtime}-{sid[:8]}" if name is None else name)
        self.events = _events(events)

    def launch(self, prompt: str, spec: Spec, resumed: bool) -> Launch:
        return self.run.launch(prompt, spec, resumed, tui.hook_argv(tui.session_dir(self.name)))

    @contextlib.contextmanager
    def prepared(self, info: SessionInfo, workdir: str) -> Iterator[tui.TuiMode]:
        with tui.lock(self.name):
            yield tui.TuiMode.prepare(self.name, self.run, info, self.events, runtime=self.runtime, workdir=workdir)

    def begin(self, host: tui.TuiMode) -> None:
        launch = self.command
        host.begin(launch.argv, cwd=self.cwd, env=_child_env(launch, self.cwd), status_line=self.settings.status_line)


class _InProcessSteps:
    name = None

    @staticmethod
    def run_of(adapter: Adapter) -> InProcess | None:
        return adapter.in_process()

    def __init__(self, run: InProcess, runtime: str, sid: str, name, events):
        _no_tui_keys(name, events)
        self.run = run

    def info(self, prompt: str, spec: Spec, resumed: bool) -> SessionInfo:
        self.run.check(prompt, spec, resumed)
        self.args = prompt, spec, resumed
        return SessionInfo(spec.sid, "", spec.access.cwd or spec.workdir, spec.access.project, "", "")

    def prepared(self, info: SessionInfo, workdir: str) -> contextlib.AbstractContextManager[in_process.InProcessMode]:
        return contextlib.nullcontext(in_process.InProcessMode(self.run))

    def begin(self, host: in_process.InProcessMode) -> None:
        host.begin(*self.args)


_STEPS = {"headless": _HeadlessSteps, "tui": _TuiSteps, "in_process": _InProcessSteps}


def _idle(idle_timeout) -> None:
    if idle_timeout is not None and (isinstance(idle_timeout, bool) or not isinstance(idle_timeout, (int, float))
                                     or not idle_timeout > 0):
        raise ConfigError(f"idle_timeout must be a positive number of seconds, got {idle_timeout!r}")


def _name(name) -> str:
    if not isinstance(name, str) or not tmux_host.NAME.fullmatch(name):
        raise ConfigError(f"name must match [A-Za-z0-9_-]+, got {name!r:.80}")
    return name


def _events(events) -> str | None:
    if events is None:
        return None
    if not isinstance(events, str) or not events.isprintable():
        raise ConfigError(f"events must be a printable path, got {events!r:.80}")
    try:
        return tmux_host.events_file(events)
    except tmux_host.TuiError as e:
        raise ConfigError(str(e)) from None


def attach(name: str, idle_timeout: float | None = None) -> Handle:
    """Tui session `name`'s Handle from any process: its reply the last turn's, its events from the next hook record."""
    _name(name)
    _idle(idle_timeout)
    state = tui.read_state(name)
    try:
        run = registry.adapter(state["runtime"]).tui()
    except ConfigError as e:
        raise DriverError(f"no tui session {name}: {e}") from None
    if run is None:
        raise DriverError(f"no tui session {name}: runtime {state['runtime']!r} has no tui mode")
    host = tui.TuiMode.attach(name, state, run)
    info = SessionInfo(state["sid"], host.bound, state["cwd"], state["project"], state["transcript"], state["resume"])
    return Handle(state["sid"], state["runtime"], "tui", host=host, workdir=state["workdir"], info=info,
                  idle_timeout=idle_timeout, name=name, reply=host.reply)


def sessions(prefix: str = "") -> Sessions:
    if not isinstance(prefix, str) or prefix and not tmux_host.NAME.fullmatch(prefix):
        raise ConfigError(f"prefix must match [A-Za-z0-9_-]*, got {prefix!r:.80}")
    found = tmux_host.sessions(prefix)
    return Sessions(prefix, None if found is None else tuple(found))


def close(name: str) -> None:
    """Kills tui session `name` (absent is fine), then removes its session dir."""
    tui.close(_name(name))


def _placement(split, split_from, opener=None) -> None:
    if split is not None and split not in tmux_host.SPLITS:
        raise ConfigError(f"split must be right or below, got {split!r:.80}")
    if split_from is not None and not (isinstance(split_from, str) and tmux_host.NAME.fullmatch(split_from)):
        raise ConfigError(f"split_from must match [A-Za-z0-9_-]+, got {split_from!r:.80}")
    if opener is not None and not (isinstance(opener, str) and tmux_host.OPENER.fullmatch(opener)):
        raise ConfigError(f"opener must be a tmux session name or $ITERM_SESSION_ID's value, got {opener!r:.80}")


def show(name: str, split: str | None = None, split_from: str | None = None, opener: str | None = None) -> str | None:
    """Opens a pane on session `name` per the tui settings; None, else logs the reason and returns
    attach_command(name)."""
    _name(name)
    _placement(split, split_from, opener)
    settings = registry.tui()
    if tmux_host.status(name) is None:
        raise DriverError(f"no tmux session {name}")
    why = tmux_host.open_pane(name, split=split, split_from=split_from, opener=opener, per_column=settings.per_column,
                              retile=settings.retile)
    if why is None:
        return None
    LOG.warning("show %s: %s", name, why)
    return tmux_host.attach_command(name)


def layout(split: str | None = None, split_from: str | None = None) -> str | None:
    """show()'s opener once the pane it goes beside (split_from's, else the caller's) is found; None only with
    split_from."""
    _placement(split, split_from)
    if split_from is not None and tmux_host.status(split_from) is None:
        raise ConfigError(f"no tmux session {split_from}")
    try:
        tmux_host.anchor(split_from)
        try:
            return tmux_host.opener()
        except tmux_host.TuiError:
            if split_from is None:
                raise
            return None
    except tmux_host.TuiError as e:
        hint = "" if split_from else ": run from tmux or iTerm2, or pass split_from"
        raise DriverError(f"no pane to show the TUI beside ({e}){hint}") from None


def attach_command(name: str) -> str:
    return tmux_host.attach_command(_name(name))


def detach(name: str, argv: Sequence[str], cwd: str, env: Mapping[str, str], *, opener: str | None = None) -> None:
    """Runs argv in new tmux session `name`, SIGNALS blocked until argv unblocks them; returns once argv runs."""
    _name(name)
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise ConfigError(f"argv must be a non-empty list of str, got {argv!r:.80}")
    if not printable_abs(cwd):
        raise ConfigError(f"cwd must be a printable absolute path, got {cwd!r:.80}")
    if not os.path.isdir(cwd):
        raise ConfigError(f"cwd must be an existing directory, got {cwd!r:.80}")
    _placement(None, None, opener)
    try:
        tmux_host.detach(name, list(argv), cwd=cwd, env=dict(env), block=SIGNALS, opener=opener)
    except tmux_host.TuiError as e:
        raise DriverError(f"detach {name}: {e}") from e


def _probe_dir() -> str | None:
    path = os.path.expanduser(PROBE_DIR)
    try:
        os.makedirs(path, 0o700, exist_ok=True)
        return path if stat.S_ISDIR(os.lstat(path).st_mode) else None
    except OSError:
        return None


def usage(runtime: str) -> Usage | None:
    """None only when the probe fails: no probe, a failed start or timeout, or no reading in its output."""
    adapter = registry.adapter(runtime)
    launch = adapter.usage_probe()
    if launch is None or not launch.argv:
        return None
    cwd = launch.cwd or _probe_dir()
    if cwd is None:
        return None
    output = headless.probe(launch.argv, cwd=cwd, env=_child_env(launch, cwd), timeout=PROBE_TIMEOUT)
    if output is None:
        return None
    try:
        return adapter.usage(output)
    except Exception:
        return None


def session_info(runtime: str, sid: str, cwd: str) -> SessionStatus:
    if error := sid_error(sid):
        raise ConfigError(error)
    if not printable_abs(cwd):
        raise ConfigError(f"cwd must be a printable absolute path, got {cwd!r:.80}")
    adapter = registry.adapter(runtime)
    try:
        return adapter.session_status(sid, cwd)
    except OSError:
        return SessionStatus("unknown", None, "")
