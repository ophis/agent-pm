"""Tmux launch handoff, event hooks and terminal transport."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence

from agent_pm.session.engine.status_log import SessionStatusLog
from agent_pm.session.engine.types import Launch
from agent_pm.session.hosts.tmux.grid import (
    PER_COLUMN,
    tile,
)
from agent_pm.session.hosts.tmux.listing import listing
from agent_pm.session.hosts.tmux.placement import (
    anchor,
    open_pane,
    opener,
    own_session,
    show,
)
from agent_pm.session.hosts.tmux.transport import (
    ITERM_OPENER,
    NAME,
    OPENER,
    PANE,
    absent,
    address,
    attach_command,
    checked,
    display_error,
    error,
    read,
    run,
    session_name,
)
from agent_pm.session.hosts.tmux.types import (
    PaneStatus,
    Retile,
    Split,
    TuiError,
)

__all__ = [
    "listing",
    "read",
    "display_error",
    "HOST",
    "NAME",
    "OPENER",
    "PAUSE",
    "PER_COLUMN",
    "PaneStatus",
    "Retile",
    "Split",
    "TERMINAL_KEYS",
    "TuiError",
    "anchor",
    "attach_command",
    "open_pane",
    "opener",
    "own_session",
    "show",
    "tile",
]


PROCESS_ID = re.compile(r"[0-9]+ \$[0-9]+ %[0-9]+ [0-9]+")


USER_OPTION = re.compile(r"@[A-Za-z0-9_-]+")


HOST = "tmux"  # its [hosts.<name>] table


HANDOVER_TIMEOUT = 10


ERRPIPE = "errpipe"


POLL = 0.1


PAUSE = 0.5


MARK_TIMEOUT = 5


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


PROCESS = "#{pid} #{session_id} #{pane_id} #{pane_pid}"


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
signal.pthread_sigmask(signal.SIG_BLOCK, h.get('block', []))
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


def start(
    session: str,
    launch: Launch,
    *,
    events: str | None = None,
    status_line: bool = False,
    tags: Mapping[str, str] | None = None,
    proc=subprocess.run,
    sleep=time.sleep,
) -> None:
    """Runs argv in a new detached session that keeps its pane once argv ends, in cwd with env (its
    terminal keys the pane's, PWD cwd), tags, @events, the pane-died hook (@state dead, a dead line
    to @events), the `<session> <state>` border and the status line set in the same tmux call (an
    instant death still gets its dead line); opens no pane. A running session is refused, a dead one
    replaced; argv reaches the pane through a 0600 handover file. Returns once argv has exec'd;
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
        state = status(session, proc=proc)
        if state == PaneStatus.RUNNING:
            raise TuiError(f"session {session} is running; {attach_command(session)}")
        if isinstance(state, int):
            kill(session, proc=proc)
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
            proc,
        )
        if res.returncode:
            if error(res).startswith(
                "duplicate session:"
            ):  # another start took the name: not ours to kill
                may_run = False
            raise TuiError(f"tmux: {error(res)}")
        if not _started(path, pipe, argv[0], sleep):
            raise TuiError("the session did not start")
        may_run = False
    except OSError as e:
        raise TuiError(f"handover: {e}") from e
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)  # first, so a late wrapper finds no file to run
        if pipe is not None:
            os.close(pipe)
        if may_run:
            with contextlib.suppress(TuiError):
                kill(session, proc=proc)


def detach(
    session: str,
    launch: Launch,
    *,
    block: tuple[int, ...] = (),
    opener: str | None = None,
    display: str | None = None,
    events: str | None = None,
    record: str | None = None,
    control: str | None = None,
    proc=subprocess.run,
    sleep=time.sleep,
) -> str:
    """Runs argv as start does, `block` blocked until argv unblocks them; env's full iTerm2
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
        pipe = _handover(path, argv, cwd, env, block=[int(s) for s in block])
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
            proc,
        )
        if res.returncode:
            if error(res).startswith("duplicate session:"):
                may_run = False
                raise TuiError(f"session {session} exists")
            raise TuiError(f"tmux: {error(res)}")
        found = res.stdout.strip()
        if not PROCESS_ID.fullmatch(found):
            raise TuiError(f"tmux: bad new session {found!r:.80}")
        if not _started(path, pipe, argv[0], sleep):
            raise TuiError(f"session {session} did not start")
        may_run = False
    except OSError as e:
        raise TuiError(f"handover: {e}") from e
    else:
        return found
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)  # first, so a late wrapper finds no file to run
        if pipe is not None:
            os.close(pipe)
        if may_run:
            with contextlib.suppress(TuiError):
                kill(session, proc=proc)


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
    block: list[int] | None = None,
) -> int:
    cwd = os.path.abspath(cwd)
    child = {k: v for k, v in env.items() if k not in TERMINAL_KEYS}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(
            {
                "argv": argv,
                "cwd": cwd,
                "env": {**child, "PWD": cwd},
                **({"block": block} if block is not None else {}),
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


def mark(
    word: str,
    events: str | None,
    *,
    pane: str | None = None,
    tmux: str = "tmux",
    proc=subprocess.run,
) -> None:
    """Hook protocol: sets the pane ($TMUX_PANE when None) @state to word; done and blocked also
    append `HH:MM:SS <session> <word>` to events, an existing regular file (None: no line).
    """
    pane = os.environ.get("TMUX_PANE", "") if pane is None else pane
    if not pane:
        return
    if not PANE.fullmatch(pane):
        raise TuiError(f"bad tmux pane {pane!r}")
    _mark_tmux([tmux, "set-option", "-t", pane, "@state", word], proc)
    if word not in ("done", "blocked") or events is None:
        return
    session = _mark_tmux([tmux, "display-message", "-p", "-t", pane, "#S"], proc).strip()
    if not NAME.fullmatch(session):
        raise TuiError(f"tmux session {session!r}: want [A-Za-z0-9_-]+")
    try:
        # O_NONBLOCK: a FIFO without a reader fails instead of hanging the hook
        fd = os.open(events, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise TuiError(f"events file {events}: not a regular file owned by you")
            os.write(fd, (SessionStatusLog.line(session, word) + "\n").encode())
        finally:
            os.close(fd)
    except OSError as e:
        raise TuiError(f"events file {events}: {e.strerror}") from e


def _mark_tmux(argv: list[str], proc) -> str:
    try:
        res = proc(
            argv,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=MARK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise TuiError(f"tmux: timed out after {MARK_TIMEOUT} s") from None
    except OSError as e:
        raise TuiError(f"tmux: {e}") from e
    if res.returncode:
        raise TuiError(f"tmux: {error(res)}")
    return res.stdout


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


def status(target: str, *, proc=subprocess.run):
    """PaneStatus.RUNNING, None (target or server absent), PaneStatus.UNKNOWN, or the dead pane's
    exit status (signal n: -n).
    """
    target = address(target)
    try:
        res = run(
            [
                "display-message",
                "-p",
                "-t",
                target,
                "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}",
            ],
            proc,
        )
    except TuiError:
        return PaneStatus.UNKNOWN
    if res.returncode:
        return None if absent(res) else PaneStatus.UNKNOWN
    fields = res.stdout.rstrip("\n").split(" ")
    if fields == ["", "", ""]:  # tmux (3.7) exits 0 with empty fields for a missing session
        return None
    if len(fields) != 3 or fields[0] not in ("0", "1"):
        return PaneStatus.UNKNOWN
    dead, code, sig = fields
    if dead == "0" or not (code or sig):  # dead, but not yet reaped
        return PaneStatus.RUNNING
    if sig:  # libc's sys_signame (macOS: "kill"), else the number
        return -(int(sig) if sig.isdigit() else getattr(signal, "SIG" + sig.upper(), 0))
    return int(code)


def tmux_path() -> str | None:
    """The real path of the tmux on PATH, else None."""
    found = shutil.which("tmux")
    return None if found is None else os.path.realpath(found)


def kill(target: str, *, proc=subprocess.run) -> None:
    checked(["kill-session", "-t", address(target, "")], proc)


def send(session: str, text: str, *, proc=subprocess.run, sleep=time.sleep) -> None:
    paste(session, text, proc=proc)
    sleep(PAUSE)
    keys(session, "Enter", proc=proc)


def paste(target: str, text: str, *, proc=subprocess.run) -> None:
    """A bracketed paste through a buffer of its own, deleted even on failure."""
    buffer = f"tui-{target}-{uuid.uuid4().hex}"
    target = address(target)
    # load-buffer from stdin keeps text exact: send-keys -l lost long text's head, set-buffer a
    # trailing ';'
    checked(["load-buffer", "-b", buffer, "-"], proc, text)
    try:
        checked(["paste-buffer", "-p", "-d", "-b", buffer, "-t", target], proc)
    except TuiError:
        with contextlib.suppress(TuiError):
            run(["delete-buffer", "-b", buffer], proc)
        raise


def keys(target: str, *names: str, proc=subprocess.run) -> None:
    """Presses tmux keys (Enter, Escape, C-c); a name tmux would misread raises first."""
    target = address(target)
    for name in names:
        if name.startswith("-") or name.endswith(";"):
            raise TuiError(f"tmux would misread {name!r}")
    checked(["send-keys", "-t", target, *names], proc)


def option(target: str, name: str, *, proc=subprocess.run) -> str | None:
    """None when unset or the target absent."""
    target = address(target)
    if not USER_OPTION.fullmatch(name):
        raise TuiError(f"option {name!r}: want {USER_OPTION.pattern}")
    res = run(["show-options", "-v", "-t", target, name], proc)
    if res.returncode:
        if error(res).startswith("invalid option:") or absent(res):
            return None
        raise TuiError(f"tmux: {error(res)}")
    return res.stdout.removesuffix("\n")


def pane(session: str, *, proc=subprocess.run) -> str | None:
    target = f"={session_name(session)}:"
    res = run(["display-message", "-p", "-t", target, "#{pane_id}"], proc)
    if res.returncode:
        if absent(res):
            return None
        raise TuiError(f"tmux: {error(res)}")
    found = res.stdout.strip()
    if not found:  # tmux (3.7) exits 0 printing nothing for a missing session
        return None
    if not PANE.fullmatch(found):
        raise TuiError(f"bad tmux pane {found!r}")
    return found


def process(target: str, *, proc=subprocess.run) -> str | None:
    """PROCESS of target's pane (a session's: its active one); None when absent."""
    res = run(["display-message", "-p", "-t", address(target), PROCESS], proc)
    if res.returncode:
        if absent(res):
            return None
        raise TuiError(f"tmux: {error(res)}")
    found = res.stdout.strip()
    if " " not in found:  # tmux (3.7) exits 0 printing only #{pid} for a missing target
        return None
    if not PROCESS_ID.fullmatch(found):
        raise TuiError(f"bad tmux process {found!r:.80}")
    return found
