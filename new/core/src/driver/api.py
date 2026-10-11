"""The Driver's API, the only driver module other layers import. start() runs one agent session of a runtime (its
Adapter from src/driver/registry.py) in a Mode (MODES) and returns its Handle; every check runs before anything starts.
A headless session's `session` record goes to <workdir>/run.jsonl (records) before its process starts; Handle.events()
then streams the frozen event set (LimitHit included), Stderr and the Driver's IdleTimeout up to the Driver's Exited,
recording `client_session` and `stderr` on the way; nothing else is recorded or interpreted. usage(runtime) runs the
Adapter's usage probe and gives its reading as Usage; session_info(runtime, sid, cwd) gives the Adapter's
SessionStatus; session() is the run records' session query.

The tui Mode's surface is frozen here: start(mode="tui", name=…), attach(name), and send, state, screen and stop on a
tui session's Handle. Until the Driver's tui Mode is built, each tui path raises ConfigError("tui mode is not available
yet") before anything starts."""
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
POLL = 0.5   # seconds: the longest wait on a Mode for its next event
NO_TUI = "tui mode is not available yet"
PROBE_DIR = "~/.agent-pm/probe"   # a usage probe's cwd when its launch names none; made 0700
PROBE_TIMEOUT = 120               # seconds: the longest a usage probe runs


@dataclass(frozen=True)
class IdleTimeout:
    """A Driver event: no event for `seconds`, start()'s idle_timeout."""
    seconds: float


class DriverError(Exception):
    """A session that could not start, or a call its Mode does not take."""


class Handle:
    """One session start() began: `sid` (agent-pm's session id), `runtime` and `mode`."""

    def __init__(self, sid: str, runtime: str, mode: str, *, host: headless_run.HeadlessMode, workdir: str,
                 info: SessionInfo, idle_timeout: float | None):
        self.sid, self.runtime, self.mode = sid, runtime, mode
        self._host, self._info, self._idle = host, info, idle_timeout
        self._channel = records.path(workdir)
        self._reply: str | None = None
        self._queue: deque = deque()   # polled events not yet yielded
        self._closed = False    # both pipes have ended
        self._ended = False     # Exited yielded, or events() raised
        self._blocked = False   # since a Blocked, no event but Blocked or Stderr

    def events(self) -> Iterator[Event | Stderr | IdleTimeout]:
        """Each event in order, ending after Exited(returncode): made by the Driver once both pipes end and the
        process exits, awaited in slices of at most POLL s (an Exited from the adapter is dropped); a later call yields
        nothing. SessionBound is recorded as `client_session` (durable) and sets info().native_id; Stderr is recorded
        as `stderr` (best effort); a Reply, or a TurnEnd's non-empty reply, sets `reply`. With an idle_timeout, each
        span of that many seconds without an event yields IdleTimeout, except after a Blocked until an event other than
        Blocked or Stderr. Never stops the process on its own; an exception (the Mode's, a record write's) stops it,
        then propagates."""
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
        """Submits `text` to a tui session; a headless one raises DriverError."""
        raise DriverError("send: tui mode only")

    def state(self) -> str:
        """One of STATES: a headless session is live while its process runs, else absent."""
        return "live" if self._host.alive() else "absent"

    def screen(self) -> str:
        """A tui session's pane text; a headless one raises DriverError."""
        raise DriverError("screen: tui mode only")

    def stop(self) -> None:
        """Ends the session: kills its process group, waiting a bounded time for the process; idempotent. events()
        then ends with Exited."""
        self._host.stop()

    def info(self) -> SessionInfo:
        return self._info

    @property
    def reply(self) -> str | None:
        """The last reply's text; None before any."""
        return self._reply

    def usage(self) -> Usage | None:
        """The runtime's account usage, usage(runtime): starts a probe process."""
        return usage(self.runtime)


def _child_env(launch: Launch, cwd: str) -> dict[str, str]:
    """A child's environment: the caller's minus launch.unset, plus launch.env and PWD=cwd."""
    env = {k: v for k, v in os.environ.items() if k not in launch.unset}
    return {**env, **launch.env, "PWD": cwd}


def start(runtime: str, mode: str, prompt: str, spec: Spec, resume: str | None = None,
          idle_timeout: float | None = None, name: str | None = None, sid: str | None = None) -> Handle:
    """Starts a session of `runtime` in `mode` on `prompt` and returns its Handle. resume continues that session id;
    otherwise sid, else a new uuid4, names the new session; start() alone sets spec.sid. With access commands, the
    adapter's runtime_note follows the prompt after a blank line. Refusals (ConfigError, nothing started): a bad mode,
    idle_timeout, resume or sid, a sid other than resume, a caller-set spec.sid, a registry error, a Mode the runtime
    lacks, tui (not available yet), a name in headless mode, a launch with no command; an exception from the run's
    launch or the session record's write propagates. The child runs with _child_env's environment in launch.cwd, else
    spec.access.cwd, else spec.workdir; a failed spawn, after the session record, is a DriverError."""
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
    """A tui session's Handle, in another process, by its name."""
    raise ConfigError(NO_TUI)


def _probe_dir() -> str | None:
    """PROBE_DIR, ~ expanded, made 0700 when missing; None when it cannot be made or is a symlink or no directory."""
    path = os.path.expanduser(PROBE_DIR)
    try:
        os.makedirs(path, 0o700, exist_ok=True)
        return path if stat.S_ISDIR(os.lstat(path).st_mode) else None
    except OSError:
        return None


def usage(runtime: str) -> Usage | None:
    """`runtime`'s account usage: its Adapter's usage probe run (headless_run.probe, _child_env, at most PROBE_TIMEOUT
    s) in the launch's cwd, else PROBE_DIR, its stdout read by the Adapter's usage(); None (unknown) when the Adapter
    has no probe, the probe fails, times out or the reading raises. A registry error is a ConfigError. Writes no run
    record."""
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
    """Session `sid` of `runtime`, started in `cwd`, as its Adapter's session_status(); an OSError from it is
    SessionStatus("unknown", None, ""). Refusals (ConfigError): a bad sid, a cwd that is no printable absolute path, a
    registry error."""
    if error := sid_error(sid):
        raise ConfigError(error)
    if not (isinstance(cwd, str) and cwd.isprintable() and os.path.isabs(cwd)):
        raise ConfigError(f"cwd must be a printable absolute path, got {cwd!r:.80}")
    adapter = registry.adapter(runtime)
    try:
        return adapter.session_status(sid, cwd)
    except OSError:
        return SessionStatus("unknown", None, "")
