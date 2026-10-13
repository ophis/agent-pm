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
    from agent_pm.session.hosts.tmux.errors import types as _errors
    from agent_pm.session.hosts.tmux.model import types as _model
else:
    load_siblings = runpy.run_path(os.path.join(DIRECTORY, "loading/types.py"))["load_siblings"]
    _errors, _model = load_siblings(
        DIRECTORY,
        ("agent_pm.session.hosts.tmux.errors.types", "errors/types.py"),
        ("agent_pm.session.hosts.tmux.model.types", "model/types.py"),
    )


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
    if not _model.PANE.fullmatch(pane):
        raise _errors.TuiError.invalid_pane(pane)
    mark_tmux([tmux, "set-option", "-t", pane, "@state", word], proc)
    if word not in ("done", "blocked") or events is None:
        return
    session = mark_tmux([tmux, "display-message", "-p", "-t", pane, "#S"], proc).strip()
    if not _model.NAME.fullmatch(session):
        raise _errors.TuiError.invalid_shown_session(session)
    try:
        # O_NONBLOCK: a FIFO without a reader fails instead of hanging the hook
        fd = os.open(events, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                raise _errors.TuiError.unsafe_events(events)
            os.write(fd, f"{time.strftime('%H:%M:%S')} {session} {word}\n".encode())
        finally:
            os.close(fd)
    except OSError as e:
        raise _errors.TuiError.events_access(events, e) from e


def mark_tmux(argv: list[str], proc) -> str:
    try:
        res = proc(
            argv,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_model.MARK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise _errors.TuiError.mark_timeout(_model.MARK_TIMEOUT) from None
    except OSError as e:
        raise _errors.TuiError.command_failed(e) from e
    if res.returncode:
        raise _errors.TuiError.command_failed(tmux_error(res))
    return res.stdout


def events_file(path: str) -> str:
    """The absolute path, created 0600 when missing; TuiError unless a regular file tmux takes
    verbatim.
    """
    path = os.path.abspath(path)
    if "#" in path or path.endswith(";"):
        raise _errors.TuiError.misread_events(path)
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise _errors.TuiError.events_access(path, e) from e
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise _errors.TuiError.unsafe_events(path)
    return path


def session_decorations(session: str, events: str | None, *, status_line: bool) -> list[list[str]]:
    target = f"={session}:"
    died = _model.DIED if events is None else f"{_model.DIED} ; {_model.DEAD_EVENT}"
    return [
        *([["set-option", "-t", target, "@events", events]] if events is not None else []),
        ["set-hook", "-p", "-t", target, "pane-died", died],
        ["set-option", "-w", "-t", target, "pane-border-status", "top"],
        ["set-option", "-w", "-t", target, "pane-border-format", _model.BORDER],
        ["set-option", "-t", target, "status", "on" if status_line else "off"],
    ]


def status(target: str, *, proc=subprocess.run):
    """RUNNING, None (target or server absent), UNKNOWN, or the dead pane's exit status (signal n:
    -n).
    """
    target = target_name(target)
    try:
        res = call_tmux(
            [
                "display-message",
                "-p",
                "-t",
                target,
                "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}",
            ],
            proc,
        )
    except _errors.TuiError:
        return _model.UNKNOWN
    if res.returncode:
        return None if absent(res) else _model.UNKNOWN
    return exit_status(res.stdout)


def exit_status(output):
    fields = output.rstrip("\n").split(" ")
    if fields == ["", "", ""]:  # tmux (3.7) exits 0 with empty fields for a missing session
        return None
    if len(fields) != 3 or fields[0] not in ("0", "1"):
        return _model.UNKNOWN
    dead, code, sig = fields
    if dead == "0" or not (code or sig):  # dead, but not yet reaped
        return _model.RUNNING
    if sig:  # libc's sys_signame (macOS: "kill"), else the number
        return -(int(sig) if sig.isdigit() else getattr(signal, "SIG" + sig.upper(), 0))
    return int(code)


def listing(match=None, *, proc=subprocess.run) -> list[dict] | None:
    """Sessions named NAME that match (compiled; None: all) fullmatches, by name: tags, pane state,
    `shown` (no client: None; else the newest client's host session, "" if none or not NAME), a dead
    pane's last LAST_LINES lines; a bad or empty field None. [] when no server runs, None when tmux
    fails.
    """
    found = read_table(["list-sessions", "-F", _model.LISTING], 7, proc)
    if found is None:
        return None
    rows = sorted(
        f for f in found if _model.NAME.fullmatch(f[0]) and (match is None or match.fullmatch(f[0]))
    )
    if not rows:
        return []
    clients = read_table(["list-clients", "-F", _model.SHOWN], 3, proc)
    if clients is None:
        return None
    recent = {}
    for _, tty, session in sorted(
        (c for c in clients if c[0].isdigit() and c[1]), key=lambda c: int(c[0])
    ):
        recent[session] = tty
    hosts = {}
    if any(f[0] in recent for f in rows):
        panes = read_table(["list-panes", "-a", "-F", _model.HOSTS], 3, proc)
        if panes is None:
            return None
        hosts = {tty: session for dead, tty, session in panes if dead != "1"}
    out = []
    for name, dead, sid, runtime, state, pane, opener in rows:
        host = hosts.get(recent.get(name, ""), "")
        out.append(
            {
                "name": name,
                "sid": printable_column(sid),
                "runtime": printable_column(runtime),
                "state": printable_column(state),
                "pane": pane
                if _model.PANE.fullmatch(pane) or _model.ITERM_ID.fullmatch(pane)
                else None,
                "opener": opener if _model.OPENER.fullmatch(opener) else None,
                "dead": {"1": True, "0": False}.get(dead),
                "shown": None
                if name not in recent
                else host
                if _model.NAME.fullmatch(host)
                else "",
                "last_lines": last_lines(name, proc) if dead == "1" else None,
            }
        )
    return out


def read_table(args: list[str], width: int, proc) -> list[list[str]] | None:
    try:
        # -u: else, without a UTF-8 locale or $TMUX, tmux prints tabs as _
        res = call_tmux(["-u", *args], proc)
    except _errors.TuiError:
        return None
    if res.returncode:
        return [] if absent(res) else None
    return [f for f in (line.split("\t") for line in res.stdout.split("\n")) if len(f) == width]


def printable_column(value: str) -> str | None:
    return value if value and value.isprintable() else None


def last_lines(name: str, proc) -> str | None:
    try:
        return read(name, _model.LAST_LINES, proc=proc) or None
    except _errors.TuiError:
        return None


def tmux_path() -> str | None:
    """The real path of the tmux on PATH, else None."""
    found = shutil.which("tmux")
    return None if found is None else os.path.realpath(found)


def kill(target: str, *, proc=subprocess.run) -> None:
    checked_tmux(["kill-session", "-t", target_name(target, "")], proc)


def send(session: str, text: str, *, proc=subprocess.run, sleep=time.sleep) -> None:
    paste(session, text, proc=proc)
    sleep(_model.PAUSE)
    keys(session, "Enter", proc=proc)


def paste(target: str, text: str, *, proc=subprocess.run) -> None:
    """A bracketed paste through a buffer of its own, deleted even on failure."""
    buffer = f"tui-{target}-{uuid.uuid4().hex}"
    target = target_name(target)
    # load-buffer from stdin keeps text exact: send-keys -l lost long text's head, set-buffer a
    # trailing ';'
    checked_tmux(["load-buffer", "-b", buffer, "-"], proc, text)
    try:
        checked_tmux(["paste-buffer", "-p", "-d", "-b", buffer, "-t", target], proc)
    except _errors.TuiError:
        with contextlib.suppress(_errors.TuiError):
            call_tmux(["delete-buffer", "-b", buffer], proc)
        raise


def keys(target: str, *names: str, proc=subprocess.run) -> None:
    """Presses tmux keys (Enter, Escape, C-c); a name tmux would misread raises first."""
    target = target_name(target)
    for name in names:
        if name.startswith("-") or name.endswith(";"):
            raise _errors.TuiError.misread_argument(name)
    checked_tmux(["send-keys", "-t", target, *names], proc)


def option(target: str, name: str, *, proc=subprocess.run) -> str | None:
    """None when unset or the target absent."""
    target = target_name(target)
    if not _model.USER_OPTION.fullmatch(name):
        raise _errors.TuiError.invalid_option(name, _model.USER_OPTION.pattern)
    res = call_tmux(["show-options", "-v", "-t", target, name], proc)
    if res.returncode:
        if tmux_error(res).startswith("invalid option:") or absent(res):
            return None
        raise _errors.TuiError.command_failed(tmux_error(res))
    return res.stdout.removesuffix("\n")


def pane(session: str, *, proc=subprocess.run) -> str | None:
    target = f"={session_name(session)}:"
    res = call_tmux(["display-message", "-p", "-t", target, "#{pane_id}"], proc)
    if res.returncode:
        if absent(res):
            return None
        raise _errors.TuiError.command_failed(tmux_error(res))
    found = res.stdout.strip()
    if not found:  # tmux (3.7) exits 0 printing nothing for a missing session
        return None
    if not _model.PANE.fullmatch(found):
        raise _errors.TuiError.invalid_pane(found)
    return found


def read(target: str, lines: int | None = None, *, proc=subprocess.run) -> str:
    """The visible pane's text, or its last `lines` lines with history; trailing blank lines
    dropped.
    """
    args = ["capture-pane", "-p", "-J", "-t", target_name(target)]
    if lines is not None:
        if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
            raise _errors.TuiError.invalid_lines(lines)
        args += ["-S", f"-{lines}"]
    text = checked_tmux(args, proc).stdout.splitlines()
    while text and not text[-1].strip():
        text.pop()
    return "\n".join(text if lines is None else text[-lines:])


def attach_command(session: str) -> str:
    """`tmux attach -t '=<session>'`, after $TUI_ATTACH_PREFIX when not blank."""
    prefix = os.environ.get("TUI_ATTACH_PREFIX", "").strip()
    return f"{prefix} tmux attach -t '={session}'" if prefix else f"tmux attach -t '={session}'"


def checked_tmux(args: list[str], proc, stdin: str | None = None) -> subprocess.CompletedProcess:
    res = call_tmux(args, proc, stdin)
    if res.returncode:
        raise _errors.TuiError.command_failed(tmux_error(res))
    return res


def session_name(session: str) -> str:
    if not _model.NAME.fullmatch(session):
        raise _errors.TuiError.invalid_session_name(session)
    return session


def target_name(target: str, suffix: str = ":") -> str:
    if _model.PANE.fullmatch(target):
        return target
    if not _model.NAME.fullmatch(target):
        raise _errors.TuiError.invalid_target(target)
    return f"={target}{suffix}"


def call_tmux(args: list[str], proc, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        if stdin is not None:
            return proc(["tmux", *args], capture_output=True, text=True, input=stdin)
        return proc(["tmux", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise _errors.TuiError.command_failed(e) from e


def tmux_error(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or "").strip()


def absent(res: subprocess.CompletedProcess) -> bool:
    return bool(_model.ABSENT.fullmatch(tmux_error(res)))
