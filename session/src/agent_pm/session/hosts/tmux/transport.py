"""Validated tmux command transport."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
import uuid

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


SESSIONS = "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}"


def attach_command(session: str) -> str:
    """`tmux attach -t '=<session>'`, after $TUI_ATTACH_PREFIX when not blank."""
    prefix = os.environ.get("TUI_ATTACH_PREFIX", "").strip()
    return f"{prefix} tmux attach -t '={session}'" if prefix else f"tmux attach -t '={session}'"


def checked(args: list[str], proc, stdin: str | None = None) -> subprocess.CompletedProcess:
    res = run(args, proc, stdin)
    if res.returncode:
        raise TuiError(f"tmux: {error(res)}")
    return res


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


def run(args: list[str], proc, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        if stdin is not None:
            return proc(["tmux", *args], capture_output=True, text=True, input=stdin)
        return proc(["tmux", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise TuiError(f"tmux: {e}") from e


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


def read(target: str, lines: int | None = None, *, proc=subprocess.run) -> str:
    """The visible pane's text, or its last `lines` lines with history; trailing blank lines
    dropped.
    """
    args = ["capture-pane", "-p", "-J", "-t", address(target)]
    if lines is not None:
        if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
            raise TuiError(f"lines must be a positive int, not {lines!r}")
        args += ["-S", f"-{lines}"]
    text = checked(args, proc).stdout.splitlines()
    while text and not text[-1].strip():
        text.pop()
    return "\n".join(text if lines is None else text[-lines:])


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
        checked(["paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", target], proc)
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
    if " " not in found or re.fullmatch(r"[0-9]+  %[0-9]+ [0-9]+", found):
        # A pane can outlive its session while tmux formats its remaining identity fields.
        return None
    if not PROCESS_ID.fullmatch(found):
        raise TuiError(f"bad tmux process {found!r:.80}")
    return found
