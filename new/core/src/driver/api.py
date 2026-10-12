"""The Driver's API. start() checks everything, writes a headless session's `session` record to <workdir>/run.jsonl,
then starts it and returns its Handle. Handle.events() streams its events, Stderr and IdleTimeout (none while Blocked;
it stops nothing) up to the Driver's own Exited, recording `client_session` and `stderr`. usage(), session_info() and
session() give a runtime's Usage, a session's SessionStatus and its record. The tui surface is frozen; until the tui
Mode is built, each tui path raises ConfigError."""
import os
import stat
import time
import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, replace

from adapters.base import (RESUMABLE, Access, Activity, Blocked, ConfigError, Event, Exited, Launch, LimitHit, Reply,
                           SessionBound, SessionInfo, SessionStatus, Spec, Stderr, TurnEnd, Usage, sid_error)
from driver import records, registry
from driver.modes import headless_run
from driver.records import session

__all__ = ["Access", "Activity", "Blocked", "ConfigError", "DriverError", "Event", "Exited", "Handle", "IdleTimeout",
           "LimitHit", "MODES", "POLL", "RESUMABLE", "Reply", "STATES", "SessionBound", "SessionInfo", "SessionStatus",
           "Spec", "Stderr", "TurnEnd", "Usage", "attach", "records", "session", "session_info", "start", "usage"]

MODES = ("headless", "tui")
STATES = ("live", "absent", "unknown")
POLL = 0.5
NO_TUI = "tui mode is not available yet"
PROBE_DIR = "~/.agent-pm/probe"
PROBE_TIMEOUT = 120


@dataclass(frozen=True)
class IdleTimeout:
    seconds: float


class DriverError(Exception):
    """A failed start, or a call its Mode lacks."""


class Handle:
    def __init__(self, sid: str, runtime: str, mode: str, *, host: headless_run.HeadlessMode, workdir: str,
                 info: SessionInfo, idle_timeout: float | None):
        self.sid, self.runtime, self.mode = sid, runtime, mode
        self._host, self._info, self._idle = host, info, idle_timeout
        self._channel = records.path(workdir)
        self._reply: str | None = None
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
        raise DriverError("send: tui mode only")

    def state(self) -> str:
        return "live" if self._host.alive() else "absent"

    def screen(self) -> str:
        raise DriverError("screen: tui mode only")

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
          idle_timeout: float | None = None, name: str | None = None, sid: str | None = None) -> Handle:
    if mode not in MODES:
        raise ConfigError(f"mode must be headless or tui, got {mode!r}")
    if idle_timeout is not None and (isinstance(idle_timeout, bool) or not isinstance(idle_timeout, (int, float))
                                     or not idle_timeout > 0):
        raise ConfigError(f"idle_timeout must be a positive number of seconds, got {idle_timeout!r}")
    if resume is not None and not (isinstance(resume, str) and resume):
        raise ConfigError(f"resume must be a session id, got {resume!r:.80}")
    if sid is not None and (error := sid_error(sid)):
        raise ConfigError(error)
    if sid is not None and resume is not None and sid != resume:
        raise ConfigError(f"sid {sid!r:.80} differs from resume {resume!r:.80}")
    if spec.sid is not None:
        raise ConfigError("spec.sid is set by start(): pass sid= or resume=")
    adapter = registry.adapter(runtime)
    run = adapter.headless_run() if mode == "headless" else adapter.tui_run()
    if run is None:
        raise ConfigError(f"runtime {runtime!r} does not support {mode} mode")
    if mode == "tui":
        raise ConfigError(NO_TUI)
    if name is not None:
        raise ConfigError("name: tui mode only")
    sid = resume or sid or str(uuid.uuid4())
    spec = replace(spec, sid=sid)
    if spec.access.commands and adapter.runtime_note:
        prompt = prompt + "\n\n" + adapter.runtime_note
    launch = run.launch(prompt, spec, resume is not None)
    if not launch.argv:
        raise ConfigError(f"runtime {runtime!r} gave no command")
    cwd = launch.cwd or spec.access.cwd or spec.workdir
    os.makedirs(spec.workdir, exist_ok=True)
    records.note(records.path(spec.workdir), "session", sid=sid, cwd=cwd, project=spec.access.project,
                 transcript=launch.transcript, resume=launch.resume)
    host = headless_run.HeadlessMode(run)
    try:
        host.begin(launch.argv, cwd=cwd, env=_child_env(launch, cwd))
    except (OSError, ValueError) as e:
        raise DriverError(f"cannot start {launch.argv[0]}: {e}") from e
    return Handle(sid, runtime, mode, host=host, workdir=spec.workdir, idle_timeout=idle_timeout,
                  info=SessionInfo(sid, "", cwd, spec.access.project, launch.transcript, launch.resume))


def attach(name: str) -> Handle:
    raise ConfigError(NO_TUI)


def _probe_dir() -> str | None:
    path = os.path.expanduser(PROBE_DIR)
    try:
        os.makedirs(path, 0o700, exist_ok=True)
        return path if stat.S_ISDIR(os.lstat(path).st_mode) else None
    except OSError:
        return None


def usage(runtime: str) -> Usage | None:
    adapter = registry.adapter(runtime)
    launch = adapter.usage_probe()
    if launch is None or not launch.argv:
        return None
    cwd = launch.cwd or _probe_dir()
    if cwd is None:
        return None
    output = headless_run.probe(launch.argv, cwd=cwd, env=_child_env(launch, cwd), timeout=PROBE_TIMEOUT)
    if output is None:
        return None
    try:
        return adapter.usage(output)
    except Exception:
        return None


def session_info(runtime: str, sid: str, cwd: str) -> SessionStatus:
    if error := sid_error(sid):
        raise ConfigError(error)
    if not (isinstance(cwd, str) and cwd.isprintable() and os.path.isabs(cwd)):
        raise ConfigError(f"cwd must be a printable absolute path, got {cwd!r:.80}")
    adapter = registry.adapter(runtime)
    try:
        return adapter.session_status(sid, cwd)
    except OSError:
        return SessionStatus("unknown", None, "")
