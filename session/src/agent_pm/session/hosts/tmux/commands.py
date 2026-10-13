"""Bundled tmux commands operations, independent of the standalone facade."""

from __future__ import annotations

import contextlib
import os
import runpy
import shutil
import signal
import stat
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


if TYPE_CHECKING:
    from agent_pm.session.hosts.tmux.errors.types import (
        TuiError,
    )
    from agent_pm.session.hosts.tmux.model.types import (
        ABSENT,
        BORDER,
        DEAD_EVENT,
        DIED,
        HOSTS,
        ITERM_ID,
        LAST_LINES,
        LISTING,
        MARK_TIMEOUT,
        NAME,
        OPENER,
        PANE,
        PAUSE,
        RUNNING,
        SHOWN,
        UNKNOWN,
        USER_OPTION,
    )
else:
    _load_file = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["load_file"]
    _errors_types = _load_file(
        "agent_pm.session.hosts.tmux.errors.types", os.path.join(DIRECTORY, "errors/types.py")
    )
    TuiError = _errors_types.TuiError
    _model_types = _load_file(
        "agent_pm.session.hosts.tmux.model.types", os.path.join(DIRECTORY, "model/types.py")
    )
    ABSENT = _model_types.ABSENT
    BORDER = _model_types.BORDER
    DEAD_EVENT = _model_types.DEAD_EVENT
    DIED = _model_types.DIED
    HOSTS = _model_types.HOSTS
    ITERM_ID = _model_types.ITERM_ID
    LAST_LINES = _model_types.LAST_LINES
    LISTING = _model_types.LISTING
    MARK_TIMEOUT = _model_types.MARK_TIMEOUT
    NAME = _model_types.NAME
    OPENER = _model_types.OPENER
    PANE = _model_types.PANE
    PAUSE = _model_types.PAUSE
    RUNNING = _model_types.RUNNING
    SHOWN = _model_types.SHOWN
    UNKNOWN = _model_types.UNKNOWN
    USER_OPTION = _model_types.USER_OPTION


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
        raise TuiError.invalid_pane(pane)
    _mark_tmux([tmux, "set-option", "-t", pane, "@state", word], proc)
    if word not in ("done", "blocked") or events is None:
        return
    session = _mark_tmux([tmux, "display-message", "-p", "-t", pane, "#S"], proc).strip()
    if not NAME.fullmatch(session):
        raise TuiError.invalid_shown_session(session)
    try:
        # O_NONBLOCK: a FIFO without a reader fails instead of hanging the hook
        fd = os.open(events, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise TuiError.unsafe_events(events)
            os.write(fd, f"{time.strftime('%H:%M:%S')} {session} {word}\n".encode())
        finally:
            os.close(fd)
    except OSError as e:
        raise TuiError.events_access(events, e) from e


def _mark_tmux(argv: list[str], proc) -> str:
    try:
        res = proc(
            argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=MARK_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        raise TuiError.mark_timeout(MARK_TIMEOUT) from None
    except OSError as e:
        raise TuiError.command_failed(e) from e
    if res.returncode:
        raise TuiError.command_failed(_err(res))
    return res.stdout


def events_file(path: str) -> str:
    """The absolute path, created 0600 when missing; TuiError unless a regular file tmux takes
    verbatim.
    """
    path = os.path.abspath(path)
    if "#" in path or path.endswith(";"):
        raise TuiError.misread_events(path)
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise TuiError.events_access(path, e) from e
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise TuiError.unsafe_events(path)
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
    """RUNNING, None (target or server absent), UNKNOWN, or the dead pane's exit status (signal n:
    -n).
    """
    target = _target(target)
    try:
        res = _tmux(
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
        return UNKNOWN
    if res.returncode:
        return None if _absent(res) else UNKNOWN
    fields = res.stdout.rstrip("\n").split(" ")
    if fields == ["", "", ""]:  # tmux (3.7) exits 0 with empty fields for a missing session
        return None
    if len(fields) != 3 or fields[0] not in ("0", "1"):
        return UNKNOWN
    dead, code, sig = fields
    if dead == "0" or not (code or sig):  # dead, but not yet reaped
        return RUNNING
    if sig:  # libc's sys_signame (macOS: "kill"), else the number
        return -(int(sig) if sig.isdigit() else getattr(signal, "SIG" + sig.upper(), 0))
    return int(code)


def listing(match=None, *, proc=subprocess.run) -> list[dict] | None:
    """Sessions named NAME that match (compiled; None: all) fullmatches, by name: tags, pane state,
    `shown` (no client: None; else the newest client's host session, "" if none or not NAME), a dead
    pane's last LAST_LINES lines; a bad or empty field None. [] when no server runs, None when tmux
    fails.
    """
    found = _table(["list-sessions", "-F", LISTING], 7, proc)
    if found is None:
        return None
    rows = sorted(
        f for f in found if NAME.fullmatch(f[0]) and (match is None or match.fullmatch(f[0]))
    )
    if not rows:
        return []
    clients = _table(["list-clients", "-F", SHOWN], 3, proc)
    if clients is None:
        return None
    recent = {}
    for _, tty, session in sorted(
        (c for c in clients if c[0].isdigit() and c[1]), key=lambda c: int(c[0])
    ):
        recent[session] = tty
    hosts = {}
    if any(f[0] in recent for f in rows):
        panes = _table(["list-panes", "-a", "-F", HOSTS], 3, proc)
        if panes is None:
            return None
        hosts = {tty: session for dead, tty, session in panes if dead != "1"}
    out = []
    for name, dead, sid, runtime, state, pane, opener in rows:
        host = hosts.get(recent.get(name, ""), "")
        out.append(
            {
                "name": name,
                "sid": _printable(sid),
                "runtime": _printable(runtime),
                "state": _printable(state),
                "pane": pane if PANE.fullmatch(pane) or ITERM_ID.fullmatch(pane) else None,
                "opener": opener if OPENER.fullmatch(opener) else None,
                "dead": {"1": True, "0": False}.get(dead),
                "shown": None if name not in recent else host if NAME.fullmatch(host) else "",
                "last_lines": _last_lines(name, proc) if dead == "1" else None,
            }
        )
    return out


def _table(args: list[str], width: int, proc) -> list[list[str]] | None:
    try:
        # -u: else, without a UTF-8 locale or $TMUX, tmux prints tabs as _
        res = _tmux(["-u", *args], proc)
    except TuiError:
        return None
    if res.returncode:
        return [] if _absent(res) else None
    return [f for f in (line.split("\t") for line in res.stdout.split("\n")) if len(f) == width]


def _printable(value: str) -> str | None:
    return value if value and value.isprintable() else None


def _last_lines(name: str, proc) -> str | None:
    try:
        return read(name, LAST_LINES, proc=proc) or None
    except TuiError:
        return None


def tmux_path() -> str | None:
    """The real path of the tmux on PATH, else None."""
    found = shutil.which("tmux")
    return None if found is None else os.path.realpath(found)


def kill(target: str, *, proc=subprocess.run) -> None:
    _tmux_ok(["kill-session", "-t", _target(target, "")], proc)


def send(session: str, text: str, *, proc=subprocess.run, sleep=time.sleep) -> None:
    paste(session, text, proc=proc)
    sleep(PAUSE)
    keys(session, "Enter", proc=proc)


def paste(target: str, text: str, *, proc=subprocess.run) -> None:
    """A bracketed paste through a buffer of its own, deleted even on failure."""
    buffer = f"tui-{target}-{uuid.uuid4().hex}"
    target = _target(target)
    # load-buffer from stdin keeps text exact: send-keys -l lost long text's head, set-buffer a
    # trailing ';'
    _tmux_ok(["load-buffer", "-b", buffer, "-"], proc, text)
    try:
        _tmux_ok(["paste-buffer", "-p", "-d", "-b", buffer, "-t", target], proc)
    except TuiError:
        with contextlib.suppress(TuiError):
            _tmux(["delete-buffer", "-b", buffer], proc)
        raise


def keys(target: str, *names: str, proc=subprocess.run) -> None:
    """Presses tmux keys (Enter, Escape, C-c); a name tmux would misread raises first."""
    target = _target(target)
    for name in names:
        if name.startswith("-") or name.endswith(";"):
            raise TuiError.misread_argument(name)
    _tmux_ok(["send-keys", "-t", target, *names], proc)


def option(target: str, name: str, *, proc=subprocess.run) -> str | None:
    """None when unset or the target absent."""
    target = _target(target)
    if not USER_OPTION.fullmatch(name):
        raise TuiError.invalid_option(name, USER_OPTION.pattern)
    res = _tmux(["show-options", "-v", "-t", target, name], proc)
    if res.returncode:
        if _err(res).startswith("invalid option:") or _absent(res):
            return None
        raise TuiError.command_failed(_err(res))
    return res.stdout.removesuffix("\n")


def pane(session: str, *, proc=subprocess.run) -> str | None:
    target = f"={_name(session)}:"
    res = _tmux(["display-message", "-p", "-t", target, "#{pane_id}"], proc)
    if res.returncode:
        if _absent(res):
            return None
        raise TuiError.command_failed(_err(res))
    found = res.stdout.strip()
    if not found:  # tmux (3.7) exits 0 printing nothing for a missing session
        return None
    if not PANE.fullmatch(found):
        raise TuiError.invalid_pane(found)
    return found


def read(target: str, lines: int | None = None, *, proc=subprocess.run) -> str:
    """The visible pane's text, or its last `lines` lines with history; trailing blank lines
    dropped.
    """
    args = ["capture-pane", "-p", "-J", "-t", _target(target)]
    if lines is not None:
        if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
            raise TuiError.invalid_lines(lines)
        args += ["-S", f"-{lines}"]
    text = _tmux_ok(args, proc).stdout.splitlines()
    while text and not text[-1].strip():
        text.pop()
    return "\n".join(text if lines is None else text[-lines:])


def attach_command(session: str) -> str:
    """`tmux attach -t '=<session>'`, after $TUI_ATTACH_PREFIX when not blank."""
    prefix = os.environ.get("TUI_ATTACH_PREFIX", "").strip()
    return f"{prefix} tmux attach -t '={session}'" if prefix else f"tmux attach -t '={session}'"


def _tmux_ok(args: list[str], proc, stdin: str | None = None) -> subprocess.CompletedProcess:
    res = _tmux(args, proc, stdin)
    if res.returncode:
        raise TuiError.command_failed(_err(res))
    return res


def _name(session: str) -> str:
    if not NAME.fullmatch(session):
        raise TuiError.invalid_session_name(session)
    return session


def _target(target: str, suffix: str = ":") -> str:
    if PANE.fullmatch(target):
        return target
    if not NAME.fullmatch(target):
        raise TuiError.invalid_target(target)
    return f"={target}{suffix}"


def _tmux(args: list[str], proc, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        if stdin is not None:
            return proc(["tmux", *args], capture_output=True, text=True, input=stdin)
        return proc(["tmux", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise TuiError.command_failed(e) from e


def _err(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or "").strip()


def _absent(res: subprocess.CompletedProcess) -> bool:
    return bool(ABSENT.fullmatch(_err(res)))
