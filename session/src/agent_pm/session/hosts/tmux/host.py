"""TmuxHost, the Host on tmux, runs every tmux call through the Tmux it holds; Tmux's start and
detach hand a launch to a new session's pane (EXEC). TmuxHost's display format, FORMAT, has one
encoder (fold) and one decoder (unfold). TmuxProcess, a session it hosts, keeps its Record and
display in the tmux options @record and @display.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence

from agent_pm.session.engine.status_log import SessionStatusLog
from agent_pm.session.engine.types import (
    ActivityState,
    ConfigError,
    DriverError,
    HookCallback,
    Key,
    KeyPress,
    Launch,
    Liveness,
    NamedKey,
    Paste,
    SessionRow,
    TuiSettings,
    Word,
)
from agent_pm.session.hosts.base import Displayable, Host, HostProcess, Terminal
from agent_pm.session.hosts.tmux import grid, hook, placement, transport
from agent_pm.session.hosts.tmux.listing import listing
from agent_pm.session.hosts.tmux.transport import (
    ITERM_OPENER,
    NAME,
    OPENER,
    PROCESS,
    PROCESS_ID,
    USER_OPTION,
    attach_command,
    display_error,
    error,
    kill,
    run,
    session_name,
    status,
)
from agent_pm.session.hosts.tmux.types import Anchor, PaneStatus, Retile, Split, TuiError
from agent_pm.utils import CONFIG_NAME, Display, ProcessControl, Record, sid_error

FORMAT = "<opener>[/<split>[/<split_from>]]"
NO_PANE_HINT = ": run from tmux or iTerm2, or pass split_from"
POLL = 0.1
KILL_WAIT = 5
LOG = logging.getLogger("session")

Parts = tuple[str | None, str | None, str | None]
TMUX_KEYS = {NamedKey.PAGE_UP: "PPage", NamedKey.PAGE_DOWN: "NPage", " ": "Space", ";": "\\;"}


HANDOVER_TIMEOUT = 10


ERRPIPE = "errpipe"


DIED = "set-option @state dead"


CLOCK = f'"$(date +{SessionStatusLog.CLOCK})"'
DEAD_EVENT = (
    "run-shell -b 'echo "
    + SessionStatusLog.line("#{q:session_name}", "dead", at=CLOCK)
    + " >> #{q:@events}'"
)
EXIT_STATUS = (
    "#{?pane_dead_signal,-#{?#{m:*[!0-9]*,#{pane_dead_signal}},$(kill -l #{q:pane_dead_signal}),"
    "#{pane_dead_signal}},#{pane_dead_status}}"
)
EXITED_EVENT = (
    "run-shell 'echo "
    + SessionStatusLog.line("#{q:session_name}", "exited", EXIT_STATUS, at=CLOCK)
    + " >> #{q:@events}' ; kill-session"
)


BORDER = " #{session_name} #{@state} "


TERMINAL_KEYS = (
    "TMUX",
    "TMUX_PANE",
    "TERM",
    "COLORTERM",
    "TERM_PROGRAM",
    "TERM_PROGRAM_VERSION",
    "TERM_SESSION_ID",
    "ITERM_SESSION_ID",
    "ITERM_PROFILE",
    "LC_TERMINAL",
    "LC_TERMINAL_VERSION",
    "COLUMNS",
    "LINES",
)


EXEC = f"""import errno, json, os, signal, sys
with open(sys.argv[1]) as f: h = json.load(f)
w = os.open(sys.argv[2], os.O_WRONLY | os.O_NONBLOCK)
try:
    os.chdir(h['cwd'])
    os.unlink(sys.argv[1])
    h['env'].update((k, os.environ[k]) for k in {TERMINAL_KEYS!r} if k in os.environ)
    for s in (signal.SIGPIPE, signal.SIGXFSZ): signal.signal(s, signal.SIG_DFL)
    os.execve(h['argv'][0], h['argv'], h['env'])
except BaseException as e:
    os.write(w, b'%d' % (isinstance(e, OSError) and e.errno or errno.EINVAL))
    raise"""


def _command(argv: Sequence[str], env: Mapping[str, str]) -> list[str]:
    if not argv:
        raise TuiError("no command")
    exe = shutil.which(argv[0], path=env.get("PATH", os.defpath))
    if exe is None:
        raise TuiError(f"command not found: {argv[0]}")
    return [os.path.abspath(exe), *argv[1:]]


def _wrapper(tmp: str) -> tuple[str, list[str]]:
    path, fifo = os.path.join(tmp, "handover.json"), os.path.join(tmp, ERRPIPE)
    for arg in (sys.executable, path, fifo):
        if "#" in arg or arg.endswith(";"):
            raise TuiError(f"tmux would misread {arg!r}")
    return path, [sys.executable, "-I", "-c", EXEC, path, fifo]


def _handover(
    path: str,
    argv: list[str],
    cwd: str,
    env: Mapping[str, str],
) -> int:
    cwd = os.path.abspath(cwd)
    child = {k: v for k, v in env.items() if k not in TERMINAL_KEYS}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(
            {
                "argv": argv,
                "cwd": cwd,
                "env": {**child, "PWD": cwd},
            },
            f,
        )
    fifo = os.path.join(os.path.dirname(path), ERRPIPE)
    os.mkfifo(fifo, 0o600)
    return os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)


def _tags(session: str, tags: Mapping[str, str]) -> list[str]:
    args = []
    for key, value in tags.items():
        if key == "@display" and (why := display_error(value)):
            raise TuiError(why)
        if (
            not USER_OPTION.fullmatch(key)
            or not isinstance(value, str)
            or value.endswith(";")
            or "\0" in value
        ):
            raise TuiError(
                f"tag {key!r}={value!r}: want {USER_OPTION.pattern} and a str tmux takes verbatim"
            )
        args += [";", "set-option", "-t", f"={session}:", key, value]
    return args


def _started(path: str, pipe: int, command: str, sleep) -> bool:
    """
    Whether the handover went, then execve closed the errpipe, each in time; errpipe data raises.
    """
    polls, out = 0, b""
    while os.path.exists(path):
        if polls == round(HANDOVER_TIMEOUT / POLL):
            return False
        sleep(POLL)
        polls += 1
    polls = 0
    while True:  # only now: a FIFO no writer opened reads EOF
        try:
            chunk = os.read(pipe, 64)
        except BlockingIOError:
            if polls >= round(HANDOVER_TIMEOUT / POLL):
                return False
            sleep(POLL)
            polls += 1
            continue
        if not chunk:
            break
        out += chunk
    if out:
        try:
            reason = os.strerror(int(out))
        except (ValueError, OverflowError):
            reason = "bad errpipe data"
        raise TuiError(f"cannot run {command}: {reason}")
    return True


def validate_events(path: str) -> str:
    """Check an events destination without opening it for writing or creating it."""
    path = os.path.abspath(path)
    if "#" in path or path.endswith(";"):
        raise TuiError(f"events file {path}: tmux would misread it")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            raise TuiError(f"events file {path}: No such file or directory") from None
        if not os.access(parent, os.W_OK):
            raise TuiError(f"events file {path}: Permission denied") from None
    except OSError as error:
        raise TuiError(f"events file {path}: {error.strerror}") from error
    else:
        if stat.S_ISDIR(info.st_mode):
            raise TuiError(f"events file {path}: {os.strerror(errno.EISDIR)}")
        if stat.S_ISLNK(info.st_mode):
            raise TuiError(f"events file {path}: Too many levels of symbolic links")
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise TuiError(f"events file {path}: not a regular file owned by you")
        if not os.access(path, os.W_OK):
            raise TuiError(f"events file {path}: Permission denied")
    return path


def validate_start(
    session: str, argv: Sequence[str], env: Mapping[str, str], tags: Mapping[str, str]
) -> None:
    session_name(session)
    _tags(session, tags)
    _command(argv, env)


def events_file(path: str) -> str:
    """The absolute path, created 0600 when missing; TuiError unless a regular file tmux takes
    verbatim.
    """
    path = validate_events(path)
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise TuiError(f"events file {path}: {e.strerror}") from e
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise TuiError(f"events file {path}: not a regular file owned by you")
    return path


def _decorations(session: str, events: str | None, *, status_line: bool) -> list[list[str]]:
    target = f"={session}:"
    died = DIED if events is None else f"{DIED} ; {DEAD_EVENT}"
    return [
        *([["set-option", "-t", target, "@events", events]] if events is not None else []),
        ["set-hook", "-p", "-t", target, "pane-died", died],
        ["set-option", "-w", "-t", target, "pane-border-status", "top"],
        ["set-option", "-w", "-t", target, "pane-border-format", BORDER],
        ["set-option", "-t", target, "status", "on" if status_line else "off"],
    ]


class Tmux:
    """Every tmux call a TmuxHost makes, through proc (subprocess.run's signature) and sleep:
    production uses one Tmux(), tests inject a fake. start and detach hand a launch over to a new
    session's pane; the rest bind the tmux package's commands to proc.
    """

    PAUSE = transport.PAUSE

    def __init__(self, proc=subprocess.run, sleep=time.sleep) -> None:
        self.proc, self.sleep = proc, sleep

    def start(
        self,
        session: str,
        launch: Launch,
        *,
        events: str | None = None,
        status_line: bool = False,
        tags: Mapping[str, str] | None = None,
    ) -> None:
        """Runs argv in a new detached session that keeps its pane once argv ends, in cwd with env (its
        terminal keys the pane's, PWD cwd), tags, @events, the pane-died hook (@state dead, a dead line
        to @events), the `<session> <state>` border and the status line set in the same tmux call (an
        instant death still gets its dead line); opens no pane. An existing session is refused untouched; argv reaches the pane through a 0600 handover file. Returns once argv has exec'd;
        raising, it leaves no session of its own.
        """
        argv, cwd, env = launch.argv, launch.cwd, launch.env
        session_name(session)
        tag_options = _tags(session, tags or {})
        argv = _command(argv, env)
        if events is not None:
            events = events_file(events)
        tmp = pipe = None
        may_run = False
        try:
            tmp = tempfile.mkdtemp()
            path, wrapper = _wrapper(tmp)
            state = status(session, proc=self.proc)
            if state == PaneStatus.RUNNING:
                raise TuiError(f"session {session} is running; {attach_command(session)}")
            if isinstance(state, int):
                raise TuiError(f"session {session} exists")
            pipe = _handover(path, argv, cwd, env)
            cols, rows = shutil.get_terminal_size()
            decorations = [
                arg
                for args in _decorations(session, events, status_line=status_line)
                for arg in (";", *args)
            ]
            # new-session may have run even when the tmux call fails
            may_run = True
            res = run(
                [
                    "new-session",
                    "-d",
                    "-s",
                    session,
                    "-x",
                    str(cols),
                    "-y",
                    str(rows),
                    *wrapper,
                    ";",
                    "set-option",
                    "-p",
                    "-t",
                    f"={session}:",
                    "remain-on-exit",
                    "on",
                    *tag_options,
                    *decorations,
                ],
                self.proc,
            )
            if res.returncode:
                if error(res).startswith(
                    "duplicate session:"
                ):  # another start took the name: not ours to kill
                    may_run = False
                raise TuiError(f"tmux: {error(res)}")
            if not _started(path, pipe, argv[0], self.sleep):
                raise TuiError("the session did not start")
            may_run = False
        except OSError as e:
            raise TuiError(f"handover: {e}") from e
        finally:
            if tmp:
                shutil.rmtree(
                    tmp, ignore_errors=True
                )  # first, so a late wrapper finds no file to run
            if pipe is not None:
                os.close(pipe)
            if may_run:
                with contextlib.suppress(TuiError):
                    kill(session, proc=self.proc)

    def detach(
        self,
        session: str,
        launch: Launch,
        *,
        opener: str | None = None,
        display: str | None = None,
        events: str | None = None,
        record: str | None = None,
        control: str | None = None,
    ) -> str:
        """Runs argv as start does; env's full iTerm2
        $ITERM_SESSION_ID stays; opener, display and record (a line) become @opener, @display, @record.
        No decoration; with events, @events and a kept pane whose pane-died hook appends
        `HH:MM:SS <session> exited <rc>` (signal n: -n) to it, then kills the session. An existing
        session is refused untouched. Returns its pane's process(), read as tmux made it.
        """
        argv, cwd, env = launch.argv, launch.cwd, launch.env
        session_name(session)
        if opener is not None and not OPENER.fullmatch(opener):
            raise TuiError(
                f"invalid opener {opener!r}: want a tmux session name or $ITERM_SESSION_ID's value"
            )
        named = {
            "@kind": "detach",
            "@opener": opener,
            "@display": display,
            "@record": record,
            "@control": control,
        }
        tag_options = _tags(session, {k: v for k, v in named.items() if v is not None})
        exited = []
        if events is not None:
            target = f"={session}:"
            exited = [
                *(";", "set-option", "-p", "-t", target, "remain-on-exit", "on"),
                *(";", "set-option", "-t", target, "@events", events_file(events)),
                *(";", "set-hook", "-p", "-t", target, "pane-died", EXITED_EVENT),
            ]
        for arg in argv:
            if "\0" in arg:
                raise TuiError("an argv item holds a NUL character")
            try:
                os.fsencode(arg)
            except UnicodeEncodeError:
                raise TuiError("an argv item cannot be encoded") from None
        argv = _command(argv, env)
        iterm = env.get("ITERM_SESSION_ID", "")
        terminal = ["-e", f"ITERM_SESSION_ID={iterm}"] if ITERM_OPENER.fullmatch(iterm) else []
        tmp = pipe = None
        may_run = False
        try:
            tmp = tempfile.mkdtemp()
            path, wrapper = _wrapper(tmp)
            pipe = _handover(path, argv, cwd, env)
            may_run = True
            res = run(
                [
                    "new-session",
                    "-d",
                    "-P",
                    "-F",
                    PROCESS,
                    *terminal,
                    "-s",
                    session,
                    *wrapper,
                    *exited,
                    *tag_options,
                ],
                self.proc,
            )
            if res.returncode:
                if error(res).startswith("duplicate session:"):
                    may_run = False
                    raise TuiError(f"session {session} exists")
                raise TuiError(f"tmux: {error(res)}")
            found = res.stdout.strip()
            if not PROCESS_ID.fullmatch(found):
                raise TuiError(f"tmux: bad new session {found!r:.80}")
            if not _started(path, pipe, argv[0], self.sleep):
                raise TuiError(f"session {session} did not start")
            may_run = False
        except OSError as e:
            raise TuiError(f"handover: {e}") from e
        else:
            return found
        finally:
            if tmp:
                shutil.rmtree(
                    tmp, ignore_errors=True
                )  # first, so a late wrapper finds no file to run
            if pipe is not None:
                os.close(pipe)
            if may_run:
                with contextlib.suppress(TuiError):
                    kill(session, proc=self.proc)

    def status(self, target: str):
        return transport.status(target, proc=self.proc)

    def process(self, target: str) -> str | None:
        return transport.process(target, proc=self.proc)

    def process_status(self, target: str):
        return transport.process_status(target, proc=self.proc)

    def option(self, target: str, name: str) -> str | None:
        return transport.option(target, name, proc=self.proc)

    def kill(self, target: str) -> None:
        transport.kill(target, proc=self.proc)

    def paste(self, target: str, text: str) -> None:
        transport.paste(target, text, proc=self.proc)

    def keys(self, target: str, *names: str) -> None:
        transport.keys(target, *names, proc=self.proc)

    def press(self, target: str, *names: str) -> None:
        transport.press(target, *names, proc=self.proc)

    def set_option(self, target: str, name: str, value: str) -> None:
        transport.set_option(target, name, value, proc=self.proc)

    def send(self, session: str, text: str) -> None:
        transport.send(session, text, proc=self.proc, sleep=self.sleep)

    def read(self, target: str, lines: int | None = None) -> str:
        return transport.read(target, lines, proc=self.proc)

    def listing(self) -> list[dict] | None:
        return listing(proc=self.proc)

    def own_session(self) -> str | None:
        return placement.own_session(proc=self.proc)

    def opener(self) -> str:
        return placement.opener(proc=self.proc)

    def anchor(self, split_from: str | None = None) -> Anchor:
        return placement.anchor(split_from, proc=self.proc)

    def open_pane(
        self, session: str, parts: Parts, *, per_column: int | None, retile: str | None
    ) -> str | None:
        """A pane for session, placed by a display's parts (opener, split, split_from)."""
        opener, split, split_from = parts
        return placement.open_pane(
            session,
            opener=opener,
            split=split,
            split_from=split_from,
            per_column=per_column,
            retile=retile,
            proc=self.proc,
        )

    def run_command(self, session: str, template: str) -> str | None:
        return placement.run_command(session, template, proc=self.proc)

    def tile(self, window: str) -> None:
        grid.tile(window, proc=self.proc)

    def mark(self, word: str, events: str | None, *, pane: str) -> None:
        hook.mark(word, events, pane=pane, proc=self.proc)

    def tmux_path(self) -> str | None:
        return transport.tmux_path()

    def attach_command(self, session: str) -> str:
        return transport.attach_command(session)

    def events_file(self, path: str) -> str:
        return events_file(path)

    def validate_events(self, path: str) -> str:
        return validate_events(path)

    def validate_start(
        self, session: str, argv: Sequence[str], env: Mapping[str, str], tags: Mapping[str, str]
    ) -> None:
        validate_start(session, argv, env, tags)


def _session(name) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise DriverError(f"no tmux session {name!r:.80}")
    return name


def _checked(display) -> None:
    if why := display_error(display):
        raise ConfigError(why)


class TmuxProcess(HostProcess):
    """The captured pane and its generation's controller. A pane can set its Record and display
    options: consumers must re-validate a Record's ctl or resume before running it.
    """

    def __init__(
        self, name: str, process: str, *, tmux: Tmux, control: ProcessControl | None = None
    ) -> None:
        self.name = name
        self.tmux = tmux
        self._process = process
        _, _, self._pane, _ = process.split(" ")
        path = self._option("@control") if control is None else None
        if control is None and self._option("@kind") in ("detach", "terminal") and not path:
            raise DriverError(
                f"tmux session {name}: missing @control; close sessions before upgrading"
            )
        try:
            if control is not None:
                self._control = control
            else:
                self._control = ProcessControl.attach(path) if path else None
        except OSError as error:
            raise DriverError(f"tmux session {name}: invalid @control: {error}") from error

    @property
    def identity(self) -> str:
        return self._process

    @property
    def token(self) -> str | None:
        return None if self._control is None else self._control.path

    def publish(self, record: Record) -> None:
        if not isinstance(record, Record):
            raise ConfigError(f"record: want a Record, got {type(record).__name__}")
        if not self._own():
            raise DriverError(f"tmux session {self.name} is gone")
        try:
            self.tmux.set_option(self._pane, "@record", record.to_line())
        except TuiError as error:
            raise DriverError(f"tmux session {self.name}: {error}") from error

    def state(self) -> Liveness:
        try:
            own, value = self._pane_state()
        except DriverError:
            return Liveness.UNKNOWN
        if not own or value is None:
            return Liveness.ABSENT
        return Liveness.UNKNOWN if value == PaneStatus.UNKNOWN else Liveness.LIVE

    def returncode(self) -> int | None:
        try:
            own, value = self._pane_state()
        except DriverError:
            return None
        if not own or value is None:
            return -signal.SIGHUP
        return value if isinstance(value, int) else None

    def session_identity(self) -> tuple[str | None, str | None]:
        return self._option("@sid"), self._option("@generation")

    def activity(self) -> Word | None:
        value = self._option("@state")
        return Word(value) if value in tuple(Word) else None

    def mark(self, word: Word, events: str | None) -> None:
        if self._own():
            try:
                self.tmux.mark(word, events, pane=self._pane)
            except TuiError as error:
                raise DriverError(str(error)) from error

    def alive(self) -> bool:
        own, state = self._pane_state()
        if not own:
            return False
        if state == PaneStatus.UNKNOWN:
            raise DriverError(f"tmux session {self.name}: tmux failed")
        return state == PaneStatus.RUNNING and (self._control is None or self._control.running())

    def wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.alive() or self._group_live():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def kill(self) -> None:
        self._kill_group()
        if not self.wait_exit(KILL_WAIT):
            raise DriverError(f"process group {self.name}: did not exit")

    def close(self) -> None:
        """Closes the pane, then ends the process group, even when the pane fails; every failure
        is in the one DriverError raised.
        """
        failures = []
        for step in (self._close_pane, self._end_group):
            try:
                step()
            except DriverError as error:
                failures.append(str(error))
        if failures:
            raise DriverError("; ".join(failures))

    def _end_group(self) -> None:
        if self._control is None:
            return
        try:
            self._control.terminate()
        except (ConnectionRefusedError, ConnectionResetError):
            return
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error
        if not self._wait_group(KILL_WAIT):
            self._kill_group()
            if not self._wait_group(KILL_WAIT):
                raise DriverError(f"process group {self.name}: did not exit")

    def _wait_group(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._group_live():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def _close_pane(self) -> None:
        if not self._own():
            return
        try:
            self.tmux.kill(self._pane)
        except TuiError as e:
            if self._own():
                raise DriverError(str(e)) from None

    def record(self) -> Record | None:
        line = self._option("@record")
        if line is not None:
            with contextlib.suppress(ValueError):
                return Record.parse(line)
        return None

    def display(self) -> Display:
        return self._option("@display")

    def _pane_state(self):
        """Whether its captured pane still runs this process, and the pane's status(): one call."""
        try:
            found, value = self.tmux.process_status(self._pane)
        except TuiError as e:
            raise DriverError(f"tmux session {self.name}: {e}") from None
        return found == self._process, value

    def _own(self) -> bool:
        try:
            return self.tmux.process(self._pane) == self._process
        except TuiError as e:
            raise DriverError(f"tmux session {self.name}: {e}") from None

    def _group_live(self) -> bool:
        try:
            return self._control is not None and self._control.live()
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error

    def _kill_group(self) -> None:
        if self._control is None:
            self._close_pane()
            return
        try:
            self._control.kill()
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error

    def _option(self, key: str) -> str | None:
        if not self._own():
            return None
        try:
            return self.tmux.option(self._pane, key)
        except TuiError as e:
            raise DriverError(f"tmux session {self.name}: {e}") from None


class TmuxGroup(HostProcess):
    """A reclaimed process group whose tmux session may be gone: wait for it, kill it, close it."""

    def __init__(self, control: ProcessControl) -> None:
        self.name = os.path.basename(control.path)
        self._control = control

    @property
    def token(self) -> str | None:
        return self._control.path

    def alive(self) -> bool:
        try:
            return self._control.live()
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error

    def wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self.alive():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)
        return True

    def kill(self) -> None:
        try:
            self._control.kill()
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error
        if not self.wait_exit(KILL_WAIT):
            raise DriverError(f"process group {self.name}: did not exit")

    def close(self) -> None:
        try:
            self._control.terminate()
        except OSError as error:
            raise DriverError(f"process group {self.name}: {error}") from error
        if not self.wait_exit(KILL_WAIT):
            self.kill()

    def record(self) -> Record | None:
        return None

    def display(self) -> Display:
        return None


class TmuxTerminal(TmuxProcess, Terminal):
    _pasted = False

    def send(self, action: Paste | Key) -> None:
        if not self.alive():
            raise DriverError(f"tui session {self.name} is not running")
        try:
            if isinstance(action, Paste):
                self.tmux.paste(self._pane, action.text)
                self._pasted = True
            else:
                if self._pasted:
                    time.sleep(self.tmux.PAUSE)
                self.tmux.keys(self._pane, action.name)
                self._pasted = False
        except TuiError as error:
            raise DriverError(f"send to tui session {self.name}: {error}") from error

    def press(self, keys) -> None:
        names = [_tmux_key(key) for key in keys]
        if not self.alive():
            raise DriverError(f"tui session {self.name} is not running")
        try:
            if self._pasted:
                time.sleep(self.tmux.PAUSE)
            self.tmux.press(self._pane, *names)
            self._pasted = False
        except TuiError as error:
            raise DriverError(f"send to tui session {self.name}: {error}") from error


def _tmux_key(key) -> str:
    """KeyPress → tmux's key name: modifiers as C-, M-, S-; PageUp, PageDown, space and ; renamed."""
    if not isinstance(key, KeyPress):
        raise ConfigError(f"key {key!r:.80}: want a KeyPress")
    if len(key.key) == 1 and (
        key.shift or key.ctrl and not (key.key.isascii() and key.key.isalpha())
    ):
        raise ConfigError(f"key {key}: tmux cannot send it")  # it would send the bare character
    prefixes = ("C-" * key.ctrl, "M-" * key.meta, "S-" * key.shift)
    return "".join(prefixes) + TMUX_KEYS.get(key.key, key.key)


class TmuxHost(Host, Displayable):
    """settings: [hosts.<name>]'s workers_per_column and grid_retile shape show's grid. A display
    not in FORMAT is kept as given.
    """

    def __init__(
        self, name: str, settings: Mapping | None = None, *, tmux: Tmux | None = None
    ) -> None:
        super().__init__(name, settings)
        self.tmux = Tmux() if tmux is None else tmux

    @classmethod
    def fold(cls, opener, split, split_from) -> Display:
        """FORMAT's one encoder; None when every part is. ConfigError for a bad part."""
        if opener is not None and not (isinstance(opener, str) and OPENER.fullmatch(opener)):
            raise ConfigError(
                f"opener must be a tmux session name or $ITERM_SESSION_ID's value, got {opener!r:.80}"
            )
        if split is not None and split not in tuple(Split):
            raise ConfigError(f"split must be {' or '.join(Split)}, got {split!r:.80}")
        if split_from is not None and not (
            isinstance(split_from, str) and NAME.fullmatch(split_from)
        ):
            raise ConfigError(f"split_from must match [A-Za-z0-9_-]+, got {split_from!r:.80}")
        return "/".join((opener or "", split or "", split_from or "")).rstrip("/") or None

    @classmethod
    def unfold(cls, display) -> Parts | None:
        """FORMAT's one decoder, re-validating a display read back: (opener, split, split_from), an
        empty part None; None when display is not in FORMAT.
        """
        if display is None:
            return None, None, None
        if display_error(display):
            return None
        given = display.split("/")
        if len(given) > 3:
            return None
        opener, split, split_from = (part or None for part in (*given, "", "")[:3])
        try:
            cls.fold(opener, split, split_from)
        except ConfigError:
            return None
        return opener, split, split_from

    def detach(
        self,
        name: str,
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
    ) -> TmuxProcess:
        return self._launch(
            name, launch, display=display, events=events, record=record, terminal=False
        )

    def terminal(
        self,
        name: str,
        launch: Launch,
        *,
        display: Display = None,
        events: str | None = None,
        record: Record | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> TmuxTerminal:
        process = self._launch(
            name,
            launch,
            display=display,
            events=events,
            record=record,
            terminal=True,
            metadata=metadata,
        )
        return TmuxTerminal(name, process.identity, tmux=self.tmux, control=process._control)

    def _launch(self, name, launch, *, display, events, record, terminal, metadata=None):
        self.validate(name, launch, display=display)
        events = self.events_path(events)
        events = self.prepare_events(events)
        parts = self.unfold(display)
        control = ProcessControl.prepare(
            launch.argv, launch.cwd, env=launch.env, blocked_signals=launch.blocked_signals
        )
        process = None
        created = False
        try:
            command = Launch(tuple(control.command()), cwd=launch.cwd, env=launch.env)
            if terminal:
                self.tmux.start(
                    name,
                    command,
                    events=events,
                    status_line=self.tui_settings().status_line,
                    tags={
                        "@kind": "terminal",
                        "@control": control.path,
                        **(
                            {"@sid": record.sid, "@runtime": record.kind}
                            if record is not None and record.sid is not None
                            else {}
                        ),
                        **{
                            f"@{key}": value
                            for key, value in (metadata or {}).items()
                            if key in ("sid", "runtime", "generation")
                        },
                        **({"@display": display} if display is not None else {}),
                        **({"@opener": parts[0]} if parts is not None and parts[0] else {}),
                        **({"@record": record.to_line()} if record is not None else {}),
                    },
                )
                created = True
                process = self.tmux.process(name)
                if process is None:
                    raise TuiError(f"session {name} disappeared during start")
            else:
                process = self.tmux.detach(
                    name,
                    command,
                    opener=None if parts is None else parts[0],
                    display=display,
                    events=events,
                    record=None if record is None else record.to_line(),
                    control=control.path,
                )
            control.wait_started()
        except (TuiError, OSError) as error:
            cleanup = self._cancel_launch(control, process, name if created else None)
            detail = f"; {cleanup}" if cleanup else ""
            failure = (
                f"cannot run {error.filename or launch.argv[0]}: {os.strerror(error.errno)}"
                if isinstance(error, OSError) and error.errno is not None
                else str(error)
            )
            prefix = "" if terminal else f"detach {name}: "
            raise DriverError(f"{prefix}{failure}{detail}") from error
        return TmuxProcess(name, process, tmux=self.tmux, control=control)

    def validate(self, name: str, launch: Launch, *, display: Display = None) -> None:
        super().validate(name, launch, display=display)
        try:
            self.tmux.validate_start(name, launch.argv, launch.env, {})
        except TuiError as error:
            raise DriverError(str(error)) from error
        self.tui_settings()

    def prepare_events(self, events: str | None) -> str | None:
        try:
            return None if events is None else self.tmux.events_file(events)
        except TuiError as error:
            raise DriverError(str(error)) from error

    def events_path(self, events: str | None) -> str | None:
        try:
            return None if events is None else self.tmux.validate_events(events)
        except TuiError as error:
            raise ConfigError(str(error)) from error

    def hook_callback(self, name: str, metadata: Mapping[str, str]) -> HookCallback:
        return HookCallback(
            hook.__name__,
            {
                "executable": self._executable(),
                "target": name,
                "generation": metadata["generation"],
                "environment": {
                    key: value
                    for key, value in os.environ.items()
                    if key in ("TMUX", "TMUX_TMPDIR")
                },
            },
        )

    def _executable(self) -> str:
        path = self.tmux.tmux_path()
        if path is None:
            raise DriverError("tmux not found")
        return path

    def protected_paths(self) -> tuple[str, ...]:
        return (self._executable(),)

    def tui_settings(self) -> TuiSettings:
        line = self.settings.get("status_line", False)
        per = self.settings.get("workers_per_column")
        retile = self.settings.get("grid_retile")
        if not isinstance(line, bool):
            raise ConfigError("status_line: want true or false")
        if per is not None and (
            isinstance(per, bool) or not isinstance(per, int) or not 1 <= per <= 9999
        ):
            raise ConfigError("workers_per_column: want an integer from 1 to 9999")
        if retile is not None and retile not in tuple(Retile):
            raise ConfigError(f"grid_retile: want one of {', '.join(Retile)}")
        return TuiSettings(line, per, retile)

    def _cancel_launch(
        self, control: ProcessControl, process: str | None = None, name: str | None = None
    ) -> str:
        errors = []
        try:
            control.cancel()
        except OSError as error:
            errors.append(f"cancel controller: {error}")
        if process is not None:
            pane = process.split(" ")[2]
            try:
                if self.tmux.process(pane) == process:
                    self.tmux.kill(pane)
            except TuiError as error:
                errors.append(f"close pane: {error}")
        elif name is not None:
            try:
                if self.tmux.option(name, "@control") == control.path:
                    self.tmux.kill(name)
            except TuiError as error:
                errors.append(f"close session: {error}")
        return "; ".join(errors)

    def attach(self, name: str) -> TmuxProcess:
        state = self.tmux.status(_session(name))
        if state is None:
            raise DriverError(f"no tmux session {name}")
        if state == PaneStatus.UNKNOWN:
            raise DriverError(f"tmux session {name}: tmux failed")
        try:
            process = self.tmux.process(name)
        except TuiError as e:
            raise DriverError(f"tmux session {name}: {e}") from None
        if process is None:
            raise DriverError(f"no tmux session {name}")
        try:
            terminal = self.tmux.option(name, "@kind") == "terminal"
        except TuiError as e:
            raise DriverError(f"tmux session {name}: {e}") from None
        return (TmuxTerminal if terminal else TmuxProcess)(name, process, tmux=self.tmux)

    def list(self, display: Display = None) -> list[str]:
        if display == "":
            return []
        found = self.tmux.listing()
        if found is None:
            raise DriverError("tmux sessions unknown: tmux failed")
        parts = self.unfold(display)

        def matches(value):
            other = self.unfold(value)
            return value == display or (
                parts is not None
                and other is not None
                and parts[0] is not None
                and parts[0] == other[0]
            )

        return sorted(row["name"] for row in found if display is None or matches(row["display"]))

    def descendants(self, display: Display = None) -> list[str]:
        """list(display)'s sessions, then each session whose @opener is one of those, transitively."""
        found = set(self.list(display))
        rows = self.tmux.listing() if found else []
        if rows is None:
            raise DriverError("tmux sessions unknown: tmux failed")
        while grown := {
            row["name"] for row in rows if row["name"] not in found and row["opener"] in found
        }:
            found |= grown
        return sorted(found)

    def reclaim(self, token: str) -> TmuxGroup | None:
        """A group-only handle on token's controller (a private apm-group-* directory and
        socket of this user's) while its group lives; else None; DriverError if it cannot tell.
        """
        if not isinstance(token, str):
            return None
        try:
            control = ProcessControl.attach(token)
        except (OSError, ValueError):
            return None
        group = TmuxGroup(control)
        return group if group.alive() else None

    def rows(self, display: Display = None) -> list[SessionRow]:
        names = set(self.list(display))
        found = self.tmux.listing()
        if found is None:
            raise DriverError("tmux sessions unknown: tmux failed")
        try:
            own = self.tmux.own_session() if found else None
        except TuiError as error:
            raise DriverError(f"tmux sessions unknown: {error}") from error
        return [
            SessionRow(
                name=row["name"],
                sid=None if sid_error(row["sid"]) else row["sid"],
                runtime=row["runtime"]
                if isinstance(row["runtime"], str) and CONFIG_NAME.fullmatch(row["runtime"])
                else None,
                state=ActivityState.DEAD
                if row["dead"]
                else ActivityState(row["state"])
                if row["state"] in (Word.DONE, Word.BLOCKED)
                else ActivityState.WORKING,
                display=row["display"],
                shown=row["shown"],
                last_lines=row["last_lines"],
                own=row["name"] == own,
            )
            for row in found
            if row["name"] in names
        ]

    def validate_display(self, value: Display) -> None:
        _checked(value)

    def display(self, value: Display) -> Display:
        _checked(value)
        parts = self.unfold(value)
        if parts is None or parts[0] is not None:
            return value
        try:
            opener = self.tmux.opener()
        except TuiError:
            return value
        return self.fold(opener, parts[1], parts[2])

    def display_help(self) -> str:
        return (
            f"{FORMAT}: opener a tmux session name or $ITERM_SESSION_ID's value (empty: the "
            f"caller's), split {' or '.join(Split)}, split_from the tmux session whose "
            "pane it goes beside; any other printable value not ending in ; is kept as given"
        )

    def show(self, name: str, *, display: Display = None, command: str | None = None) -> str | None:
        if self.tmux.status(_session(name)) is None:
            raise DriverError(f"no tmux session {name}")
        try:
            why = (
                self.tmux.open_pane(
                    name,
                    self._parts(display),
                    per_column=self.settings.get("workers_per_column"),
                    retile=self.settings.get("grid_retile"),
                )
                if command is None
                else self.tmux.run_command(name, command)
            )
        except TuiError as e:
            raise ConfigError(str(e)) from None
        if why is None:
            return None
        LOG.warning("show %s: %s", name, why)
        return self.tmux.attach_command(name)

    def attach_command(self, name: str) -> str:
        return self.tmux.attach_command(_session(name))

    def screen(self, name: str | HostProcess, lines: int | None = None) -> str:
        if isinstance(name, HostProcess):
            if not isinstance(name, TmuxProcess) or name.tmux is not self.tmux or not name.alive():
                raise DriverError(f"session {name.name} is not running on host {self.name}")
            target = name._pane
        else:
            target = _session(name)
        try:
            return self.tmux.read(target, lines)
        except TuiError as e:
            raise DriverError(f"tmux session {name}: {e}") from None

    def layout(self, *, display: Display = None) -> str | None:
        """display, its opener part the caller's once the pane it goes beside (split_from's, else
        the caller's) is found; that part stays empty only beside split_from.
        """
        opener, split, split_from = self._parts(display)
        if split_from is not None and self.tmux.status(split_from) is None:
            raise ConfigError(f"no tmux session {split_from}")
        try:
            if opener is None or split_from is not None:
                self.tmux.anchor(split_from)
            if opener is None:
                try:
                    opener = self.tmux.opener()
                except TuiError:
                    if split_from is None:
                        raise
        except TuiError as e:
            raise DriverError(
                f"no pane to show the TUI beside ({e})" + ("" if split_from else NO_PANE_HINT)
            ) from None
        return self.fold(opener, split, split_from)

    def tile(self, window: str) -> None:
        try:
            self.tmux.tile(window)
        except TuiError as e:
            raise DriverError(f"tile {window}: {e}") from None

    def _parts(self, display) -> Parts:
        parts = self.unfold(display)
        if parts is None:
            raise ConfigError(f"display {display!r:.80}: not {FORMAT}")
        return parts
