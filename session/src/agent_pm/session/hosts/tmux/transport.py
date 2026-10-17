"""Tmux, tmux inside our program: every tmux call through runner (subprocess.run's signature) and
sleep, production one Tmux(), tests a fake runner. start and detach hand a launch over to a new
session's pane (EXEC); launch puts it on tmux's command line. Listings skip sessions tagged VIEW.
"""

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
from agent_pm.session.hosts.tmux.types import (
    PaneStatus,
    TuiError,
)

NAME = re.compile(r"[A-Za-z0-9_-]+")


PANE = re.compile(r"%[0-9]+")


ITERM_ID = re.compile(r"[A-Za-z0-9-]+")


ITERM_OPENER = re.compile(rf"[A-Za-z0-9]+:{ITERM_ID.pattern}")  # w0t0p0:<unique id>


OPENER = re.compile(rf"{NAME.pattern}|{ITERM_OPENER.pattern}")


SESSION_ID = re.compile(r"\$[0-9]+")


ABSENT = re.compile(
    r"can't find (session|pane).*|no server running.*|"
    r"error connecting to .* \(No such file or directory\)",
    re.S,
)


CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{client_control_mode} #{socket_path}"


PANES = "#{pane_dead} #{pane_tty} #{pane_id}"


PROCESS_ID = re.compile(r"[0-9]+ \$[0-9]+ %[0-9]+ [0-9]+")


USER_OPTION = re.compile(r"@[A-Za-z0-9_-]+")


PAUSE = 0.5


PROCESS = "#{pid} #{session_id} #{pane_id} #{pane_pid}"


DEAD = "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"


SESSIONS = "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}"


POLL = 0.1


HANDOVER_TIMEOUT = 10


ERRPIPE = "errpipe"


DIED = "set-option @state dead"


VIEW = "@view"
VIEWLESS = ["-f", f"#{{==:#{{{VIEW}}},}}"]


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
    + " >> #{q:@events}'"
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


LISTING = "#{session_name}\t#{pane_dead}\t#{@sid}\t#{@client}\t#{@state}\t#{@pane}\t#{@opener}\t#{@display}\t#{@process}"


SHOWN = "#{client_activity}\t#{client_tty}\t#{session_name}"


LISTED_PANES = "#{pane_dead}\t#{pane_tty}\t#{session_name}\t#{pane_id}"


LAST_LINES = 2000


MARK_TIMEOUT = 5


def _own_pane(name: str, dead: str, process: str, panes: dict[str, str]) -> tuple[str | None, str]:
    """The pane to read a session's last lines from, and its pane_dead."""
    if not process:
        return name, dead
    if not PROCESS_ID.fullmatch(process):
        return None, ""
    pane = process.split(" ")[2]
    return (pane, panes[pane]) if pane in panes else (None, "1")


def _printable(value: str) -> str | None:
    return value if value and value.isprintable() else None


def attach_command(session: str) -> str:
    """`tmux attach -t '=<session>'`, after $TUI_ATTACH_PREFIX when not blank."""
    prefix = os.environ.get("TUI_ATTACH_PREFIX", "").strip()
    return f"{prefix} tmux attach -t '={session}'" if prefix else f"tmux attach -t '={session}'"


def session_name(session: str) -> str:
    if not NAME.fullmatch(session):
        raise TuiError(f"invalid session name {session!r}: want [A-Za-z0-9_-]+")
    return session


def address(target: str, suffix: str = ":") -> str:
    if PANE.fullmatch(target):
        return target
    if not NAME.fullmatch(target):
        raise TuiError(
            f"invalid target {target!r}: want a session name [A-Za-z0-9_-]+ or a pane id %N"
        )
    return f"={target}{suffix}"


def error(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or "").strip()


def absent(res: subprocess.CompletedProcess) -> bool:
    return bool(ABSENT.fullmatch(error(res)))


def display_error(value) -> str | None:
    """Why value cannot be a display (None: it can): one is None or printable text not ending in ;,
    so a tab-separated listing row stays one row.
    """
    if value is None or (
        isinstance(value, str) and value.isprintable() and not value.endswith(";")
    ):
        return None
    return f"display {value!r:.80}: want printable text not ending in ;"


def tmux_path() -> str | None:
    """The real path of the tmux on PATH, else None."""
    found = shutil.which("tmux")
    return None if found is None else os.path.realpath(found)


def _out_of_mode(target: str, *command: str) -> list[str]:
    """command after `copy-mode -q` in one tmux call: a pane in copy-mode (scrolled back) or another
    mode would swallow its keys.
    """
    return ["copy-mode", "-q", "-t", target, ";", *command]


def _process_id(found: str) -> str | None:
    if " " not in found or re.fullmatch(r"[0-9]+  %[0-9]+ [0-9]+", found):
        # A pane can outlive its session while tmux formats its remaining identity fields.
        return None
    if not PROCESS_ID.fullmatch(found):
        raise TuiError(f"bad tmux process {found!r:.80}")
    return found


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


def _recorded(session: str) -> list[str]:
    """The creating call's last command: @process, its new pane's PROCESS."""
    return [";", "set-option", "-F", "-t", f"={session}:", "@process", PROCESS]


def _identity(res: subprocess.CompletedProcess) -> str:
    found = res.stdout.strip()
    if not PROCESS_ID.fullmatch(found):
        raise TuiError(f"tmux: bad new session {found!r:.80}")
    return found


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
        info = os.stat(path)
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
        if not stat.S_ISREG(info.st_mode):
            raise TuiError(f"events file {path}: not a regular file")
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
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK, 0o600)
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise TuiError(f"events file {path}: {e.strerror}") from e
    if not stat.S_ISREG(st.st_mode):
        raise TuiError(f"events file {path}: not a regular file")
    return path


def _decorations(
    session: str, events: str | None, *, status_line: bool, died: str = DIED
) -> list[list[str]]:
    target = f"={session}:"
    died = died if events is None else f"{died} ; {DEAD_EVENT}"
    return [
        *([["set-option", "-t", target, "@events", events]] if events is not None else []),
        ["set-hook", "-p", "-t", target, "pane-died", died],
        ["set-option", "-w", "-t", target, "pane-border-status", "top"],
        ["set-option", "-w", "-t", target, "pane-border-format", BORDER],
        ["set-option", "-t", target, "status", "on" if status_line else "off"],
    ]


class Tmux:
    PAUSE = PAUSE

    def __init__(self, runner=subprocess.run, *, sleep=time.sleep) -> None:
        self.runner, self.sleep = runner, sleep

    def checked(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        res = self.run(args, stdin)
        if res.returncode:
            raise TuiError(f"tmux: {error(res)}")
        return res

    def run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        try:
            if stdin is not None:
                return self.runner(["tmux", *args], capture_output=True, text=True, input=stdin)
            return self.runner(
                ["tmux", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL
            )
        except OSError as e:
            raise TuiError(f"tmux: {e}") from e

    def read(self, target: str, lines: int | None = None) -> str:
        """The visible pane's text, or its last `lines` lines with history; trailing blank lines
        dropped.
        """
        args = ["capture-pane", "-p", "-J", "-t", address(target)]
        if lines is not None:
            if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
                raise TuiError(f"lines must be a positive int, not {lines!r}")
            args += ["-S", f"-{lines}"]
        text = self.checked(args).stdout.splitlines()
        while text and not text[-1].strip():
            text.pop()
        return "\n".join(text if lines is None else text[-lines:])

    def status(self, target: str):
        """PaneStatus.RUNNING, None (target or server absent), PaneStatus.UNKNOWN, or the dead
        pane's exit status (signal n: -n).
        """
        target = address(target)
        try:
            res = self.run(["display-message", "-p", "-t", target, DEAD])
        except TuiError:
            return PaneStatus.UNKNOWN
        if res.returncode:
            return None if absent(res) else PaneStatus.UNKNOWN
        return self._dead_status(res.stdout.rstrip("\n"))

    def process_status(self, target: str):
        """(process(), status()) of target's pane from one tmux call; (None, None) when absent."""
        res = self.run(["display-message", "-p", "-t", address(target), f"{PROCESS}\t{DEAD}"])
        if res.returncode:
            if absent(res):
                return None, None
            raise TuiError(f"tmux: {error(res)}")
        identity, _, dead = res.stdout.rstrip("\n").partition("\t")
        found = _process_id(identity.strip())
        return found, None if found is None else self._dead_status(dead)

    def _dead_status(self, text: str):
        fields = text.split(" ")
        if fields == ["", "", ""]:  # tmux (3.7) exits 0 with empty fields for a missing session
            return None
        if len(fields) != 3 or fields[0] not in ("0", "1"):
            return PaneStatus.UNKNOWN
        dead, code, sig = fields
        if dead == "0":
            return PaneStatus.RUNNING
        if not (code or sig):
            # dead, not reaped: tmux 3.4 with utempter can lose the SIGCHLD, then reaps (and runs
            # pane-died) only when another child exits; a no-op job is one
            with contextlib.suppress(TuiError):
                self.run(["run-shell", "-b", "true"])
            return PaneStatus.RUNNING
        if sig:  # libc's sys_signame (macOS: "kill"), else the number
            return -(int(sig) if sig.isdigit() else getattr(signal, "SIG" + sig.upper(), 0))
        return int(code)

    def kill(self, target: str) -> None:
        """Kills the pane a pane id names, else the named session."""
        if PANE.fullmatch(target):
            self.checked(["kill-pane", "-t", target])
        else:
            self.checked(["kill-session", "-t", address(target, "")])

    def paste(self, target: str, text: str) -> None:
        """A bracketed paste through a buffer of its own, deleted even on failure."""
        buffer = f"tui-{target}-{uuid.uuid4().hex}"
        target = address(target)
        # load-buffer from stdin keeps text exact: send-keys -l lost long text's head, set-buffer a
        # trailing ';'
        self.checked(["load-buffer", "-b", buffer, "-"], text)
        try:
            self.checked(
                _out_of_mode(target, "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", target)
            )
        except TuiError:
            with contextlib.suppress(TuiError):
                self.run(["delete-buffer", "-b", buffer])
            raise

    def keys(self, target: str, *names: str) -> None:
        """Presses tmux keys (Enter, Escape, C-c); a name tmux would misread raises first."""
        target = address(target)
        for name in names:
            if name.startswith("-") or name.endswith(";"):
                raise TuiError(f"tmux would misread {name!r}")
        self.checked(_out_of_mode(target, "send-keys", "-t", target, *names))

    def press(self, target: str, *names: str) -> None:
        """Presses tmux key names after `--` (a `-` key is no flag); `;` must come escaped
        (`\\;`).
        """
        target = address(target)
        for name in names:
            if not name or name.endswith(";") and not name.endswith("\\;"):
                raise TuiError(f"tmux would misread {name!r}")
        self.checked(_out_of_mode(target, "send-keys", "-t", target, "--", *names))

    def set_option(self, target: str, name: str, value: str) -> None:
        """Sets user option `name` of target's session."""
        target = address(target)
        if not USER_OPTION.fullmatch(name):
            raise TuiError(f"option {name!r}: want {USER_OPTION.pattern}")
        if value.endswith(";") or "\n" in value:
            raise TuiError(f"tmux would misread {value!r:.80}")
        self.checked(["set-option", "-t", target, name, value])

    def option(self, target: str, name: str) -> str | None:
        """None when unset or the target absent."""
        target = address(target)
        if not USER_OPTION.fullmatch(name):
            raise TuiError(f"option {name!r}: want {USER_OPTION.pattern}")
        res = self.run(["show-options", "-v", "-t", target, name])
        if res.returncode:
            if error(res).startswith("invalid option:") or absent(res):
                return None
            raise TuiError(f"tmux: {error(res)}")
        return res.stdout.removesuffix("\n")

    def process(self, target: str) -> str | None:
        """PROCESS of target's pane (a session's: its active one); None when absent."""
        res = self.run(["display-message", "-p", "-t", address(target), PROCESS])
        if res.returncode:
            if absent(res):
                return None
            raise TuiError(f"tmux: {error(res)}")
        return _process_id(res.stdout.strip())

    def sessions(self) -> list[str] | None:
        """The sessions' names: [] when no server runs, None when tmux fails."""
        found = self._table(["list-sessions", *VIEWLESS, "-F", "#{session_name}"], 1)
        return None if found is None else [name for (name,) in found if name]

    def listing(self, match=None) -> list[dict] | None:
        """Sessions named NAME that match (compiled; None: all) fullmatches, by name: tags, pane
        state, `shown` (no client: None; else the newest client's host session, "" if none or not
        NAME), a dead pane's last LAST_LINES lines; a bad or empty field None, a row of the wrong
        width dropped. [] when no server runs, None when tmux fails. A session with @process is read
        through the pane its launch recorded, whichever pane is active: dead once that pane is gone,
        and dead None for a bad @process; any other session through its active pane.
        """
        found = self._table(["list-sessions", *VIEWLESS, "-F", LISTING], 9)
        if found is None:
            return None
        rows = sorted(
            f for f in found if NAME.fullmatch(f[0]) and (match is None or match.fullmatch(f[0]))
        )
        if not rows:
            return []
        clients = self._table(["list-clients", "-F", SHOWN], 3)
        if clients is None:
            return None
        recent = {}
        for _, tty, session in sorted(
            (c for c in clients if c[0].isdigit() and c[1]), key=lambda c: int(c[0])
        ):
            recent[session] = tty
        hosts, launched = {}, {}
        if any(f[0] in recent or f[8] for f in rows):
            panes = self._table(["list-panes", "-a", "-F", LISTED_PANES], 4)
            if panes is None:
                return None
            hosts = {tty: session for dead, tty, session, _ in panes if dead != "1"}
            launched = {pane_id: dead for dead, _, _, pane_id in panes}
        out = []
        for name, dead, sid, client, state, pane, parent, display, process in rows:
            target, dead = _own_pane(name, dead, process, launched)
            tty = recent.get(name)
            host = "" if tty is None else hosts.get(tty, "")
            out.append(
                {
                    "name": name,
                    "sid": _printable(sid),
                    "client": _printable(client),
                    "state": _printable(state),
                    "pane": pane if PANE.fullmatch(pane) or ITERM_ID.fullmatch(pane) else None,
                    "opener": parent if OPENER.fullmatch(parent) else None,
                    "display": display if display and not display_error(display) else None,
                    "dead": {"1": True, "0": False}.get(dead),
                    "shown": None if name not in recent else host if NAME.fullmatch(host) else "",
                    "last_lines": self._last_lines(target) if dead == "1" and target else None,
                }
            )
        return out

    def _table(self, args: list[str], width: int) -> list[list[str]] | None:
        try:
            # -u: else, without a UTF-8 locale or $TMUX, tmux prints tabs as _
            res = self.run(["-u", *args])
        except TuiError:
            return None
        if res.returncode:
            return [] if absent(res) else None
        return [f for f in (line.split("\t") for line in res.stdout.split("\n")) if len(f) == width]

    def _last_lines(self, target: str) -> str | None:
        try:
            return self.read(target, LAST_LINES) or None
        except TuiError:
            return None

    def mark(
        self,
        word: str,
        events: str | None,
        *,
        pane: str | None = None,
        tmux: str = "tmux",
        session: str | None = None,
    ) -> None:
        """Hook protocol: sets the pane ($TMUX_PANE when None) @state to word; done and blocked also
        append `HH:MM:SS <session> <word>` to events, an existing regular file (None: no line); the
        session is the pane's unless given.
        """
        pane = os.environ.get("TMUX_PANE", "") if pane is None else pane
        if not pane:
            return
        if not PANE.fullmatch(pane):
            raise TuiError(f"bad tmux pane {pane!r}")
        self._mark_tmux([tmux, "set-option", "-t", pane, "@state", word])
        if word not in ("done", "blocked") or events is None:
            return
        if session is None:
            session = self._mark_tmux([tmux, "display-message", "-p", "-t", pane, "#S"]).strip()
        if not NAME.fullmatch(session):
            raise TuiError(f"tmux session {session!r}: want [A-Za-z0-9_-]+")
        try:
            # O_NONBLOCK: a FIFO without a reader fails instead of hanging the hook
            fd = os.open(events, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise TuiError(f"events file {events}: not a regular file")
                os.write(fd, (SessionStatusLog.line(session, word) + "\n").encode())
            finally:
                os.close(fd)
        except OSError as e:
            raise TuiError(f"events file {events}: {e.strerror}") from e

    def _mark_tmux(self, argv: list[str]) -> str:
        try:
            res = self.runner(
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

    def launch(
        self,
        session: str,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        tags: Mapping[str, str] | None = None,
        died: str = DIED,
    ) -> str:
        """start's session, decorations and @process in one call, its argv run as given (no PATH
        lookup, no handover file) with only env's -e values, and pane-died `died`; no events.
        Returns its pane's process().
        """
        session_name(session)
        tag_options = _tags(session, tags or {})
        if not argv:
            raise TuiError("no command")
        if not os.path.isabs(cwd) or "#" in cwd or cwd.endswith(";"):
            raise TuiError(f"tmux would misread cwd {cwd!r:.80}")
        for arg in argv:
            if arg.endswith(";"):
                raise TuiError(f"tmux would misread argv item {arg!r:.80}")
        values = []
        for key, value in env.items():
            if value.endswith(";"):
                raise TuiError(f"tmux would misread env {key}")
            values += ["-e", f"{key}={value}"]
        cols, rows = shutil.get_terminal_size()
        decorations = [
            arg
            for args in _decorations(session, None, status_line=False, died=died)
            for arg in (";", *args)
        ]
        res = self.run(
            [
                "new-session",
                "-d",
                "-P",
                "-F",
                PROCESS,
                "-s",
                session,
                "-x",
                str(cols),
                "-y",
                str(rows),
                "-c",
                cwd,
                *values,
                "--",
                *argv,
                *(";", "set-option", "-p", "-t", f"={session}:", "remain-on-exit", "on"),
                *tag_options,
                *decorations,
                *_recorded(session),
            ]
        )
        if res.returncode:
            raise TuiError(f"tmux: {error(res)}")
        return _identity(res)

    def start(
        self,
        session: str,
        launch: Launch,
        *,
        events: str | None = None,
        status_line: bool = False,
        tags: Mapping[str, str] | None = None,
    ) -> str:
        """Runs argv in a new detached session that keeps its pane once argv ends, in cwd with env (its
        terminal keys the pane's, PWD cwd), tags, @events, the pane-died hook (@state dead, a dead line
        to @events), the `<session> <state>` border, the status line and @process set in the same tmux
        call (an instant death still gets its dead line); opens no pane. An existing session is
        refused untouched; argv reaches the pane through a 0600 handover file. Returns its pane's
        process(), read as tmux made it, once argv has exec'd; raising, it leaves no session of its
        own.
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
            state = self.status(session)
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
            res = self.run(
                [
                    "new-session",
                    "-d",
                    "-P",
                    "-F",
                    PROCESS,
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
                    *_recorded(session),
                ],
            )
            if res.returncode:
                if error(res).startswith(
                    "duplicate session:"
                ):  # another start took the name: not ours to kill
                    may_run = False
                raise TuiError(f"tmux: {error(res)}")
            found = _identity(res)
            if not _started(path, pipe, argv[0], self.sleep):
                raise TuiError("the session did not start")
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
                    self.kill(session)

    def detach(
        self,
        session: str,
        launch: Launch,
        *,
        opener: str | None = None,
        display: str | None = None,
        events: str | None = None,
        record: str | None = None,
        sid: str | None = None,
        control: str | None = None,
    ) -> str:
        """Runs argv as start does; env's full iTerm2
        $ITERM_SESSION_ID stays; opener, display and record (a line) become @opener, @display, @record.
        No decoration; with events, @events and a kept pane whose pane-died hook appends
        `HH:MM:SS <session> exited <rc>` (signal n: -n) to it, then kills that pane. @process set
        in the same tmux call. An existing session is refused untouched. Returns its pane's
        process(), read as tmux made it.
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
            "@sid": sid,
            "@control": control,
        }
        tag_options = _tags(session, {k: v for k, v in named.items() if v is not None})
        exited = []
        if events is not None:
            target = f"={session}:"
            # -F stores the launched pane's id (## keeps the event's formats for the hook): a bare
            # kill-pane, its pane closed during run-shell, kills another session's pane
            on_exit = EXITED_EVENT.replace("#", "##") + " ; kill-pane -t #{pane_id}"
            exited = [
                *(";", "set-option", "-p", "-t", target, "remain-on-exit", "on"),
                *(";", "set-option", "-t", target, "@events", events_file(events)),
                *(";", "set-option", "-p", "-F", "-t", target, "pane-died", on_exit),
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
            res = self.run(
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
                    *_recorded(session),
                ],
            )
            if res.returncode:
                if error(res).startswith("duplicate session:"):
                    may_run = False
                    raise TuiError(f"session {session} exists")
                raise TuiError(f"tmux: {error(res)}")
            found = _identity(res)
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
                    self.kill(session)
