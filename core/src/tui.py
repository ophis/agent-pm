"""Generic tmux host: runs a command in a detached tmux session another agent or a person can watch and drive.

One file, tmux plus the Python stdlib (3.9+): copy it anywhere.

start(session, argv, cwd=, env=)  a detached session running argv; the command line, cwd and env reach the pane
                                  through a 0600 handover file, never through tmux
status(session)                   None (no such session), RUNNING, or the dead pane's exit status (signal n: 128+n)
kill(session)                     ends the session
Session names are [A-Za-z0-9_-]+. Errors raise TuiError.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

NAME = re.compile(r"[A-Za-z0-9_-]+")
RUNNING = "running"
HANDOVER_TIMEOUT = 10
POLL = 0.1   # seconds between checks that the wrapper took the handover file
TERMINAL_KEYS = ("TMUX", "TMUX_PANE", "TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "TERM_SESSION_ID",
                 "ITERM_SESSION_ID", "ITERM_PROFILE", "LC_TERMINAL", "LC_TERMINAL_VERSION", "COLUMNS", "LINES")
# Run in the pane as `python -I -c EXEC <file>`. tmux would misread a '#' or a trailing ';', so it has neither.
# Python ignores SIGPIPE and SIGXFSZ, and execve keeps that: reset them, as Popen does.
EXEC = "\n".join([
    "import json, os, signal, sys",
    "path = sys.argv[1]",
    "with open(path) as f:",
    "    h = json.load(f)",
    "os.unlink(path)",
    "os.chdir(h['cwd'])",
    f"h['env'].update((k, os.environ[k]) for k in {TERMINAL_KEYS!r} if k in os.environ)",
    "signal.signal(signal.SIGPIPE, signal.SIG_DFL)",
    "signal.signal(signal.SIGXFSZ, signal.SIG_DFL)",
    "os.execve(h['argv'][0], h['argv'], h['env'])",
])
STATUS = "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"


class TuiError(Exception):
    pass


def start(session: str, argv: list[str], *, cwd: str, env: dict[str, str], show: str | None = None,
          split: str = "right", beside: str | None = None, proc=subprocess.run, sleep=time.sleep) -> None:
    """Run argv in a new detached session, in cwd with env plus the pane's terminal keys; show it once started."""
    _name(session)
    if not argv:
        raise TuiError("no command")
    exe = shutil.which(argv[0], path=env.get("PATH", os.defpath))
    if exe is None:
        raise TuiError(f"command not found: {argv[0]}")
    tmp = None
    try:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "handover.json")
        for arg in (sys.executable, path):
            if "#" in arg or arg.endswith(";"):
                raise TuiError(f"tmux would misread {arg!r}")
        state = status(session, proc=proc)
        if state == RUNNING:
            raise TuiError(f"session {session} is running; tmux attach -t '={session}'")
        if state is not None:
            kill(session, proc=proc)
        handover = {"argv": [os.path.abspath(exe), *argv[1:]], "cwd": os.path.abspath(cwd),
                    "env": {k: v for k, v in env.items() if k not in TERMINAL_KEYS}}
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump(handover, f)
        cols, rows = shutil.get_terminal_size()
        res = _tmux(["new-session", "-d", "-s", session, "-x", str(cols), "-y", str(rows),
                     sys.executable, "-I", "-c", EXEC, path,
                     ";", "set-option", "-p", "-t", f"={session}:", "remain-on-exit", "on"], proc)
        if res.returncode:
            raise TuiError(f"tmux: {_err(res)}")
        if not _handed_over(path, sleep):
            shutil.rmtree(tmp, ignore_errors=True)   # first, so a late wrapper finds no file to run
            with contextlib.suppress(TuiError):
                kill(session, proc=proc)
            raise TuiError("the session did not start")
    except OSError as e:
        raise TuiError(f"handover: {e}") from e
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    globals()["show"](session, show, split=split, beside=beside, proc=proc)   # the parameter shadows show()


def status(session: str, *, proc=subprocess.run):
    """None (no such session), RUNNING, or the dead pane's exit status (signal n: 128+n)."""
    res = _tmux(["display-message", "-p", "-t", f"={_name(session)}:", STATUS], proc)
    fields = res.stdout.rstrip("\n").split(" ") if res.returncode == 0 else []
    if len(fields) != 3 or fields[0] not in ("0", "1"):   # tmux (3.7) exits 0 with empty fields for a missing session
        return None
    dead, code, sig = fields
    if dead == "0" or not (code or sig):   # dead, but not yet reaped
        return RUNNING
    if sig:   # a name where libc has sys_signame (macOS: "kill"), else the number
        return 128 + (int(sig) if sig.isdigit() else getattr(signal, "SIG" + sig.upper(), 0))
    return int(code)


def kill(session: str, *, proc=subprocess.run) -> None:
    """End the session."""
    res = _tmux(["kill-session", "-t", f"={_name(session)}"], proc)
    if res.returncode:
        raise TuiError(f"tmux: {_err(res)}")


def show(session: str, template: str | None = None, *, split: str = "right", beside: str | None = None,
         proc=subprocess.run) -> str | None:
    """Print how to attach to the session."""
    print(f"tui: session {session}: tmux attach -t '={session}'", file=sys.stderr)


def _name(session: str) -> str:
    if not NAME.fullmatch(session):
        raise TuiError(f"invalid session name {session!r}: want [A-Za-z0-9_-]+")
    return session


def _tmux(args: list[str], proc) -> subprocess.CompletedProcess:
    try:
        return proc(["tmux", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise TuiError(f"tmux: {e}") from e


def _err(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or "").strip()


def _handed_over(path: str, sleep) -> bool:
    for _ in range(round(HANDOVER_TIMEOUT / POLL)):
        if not os.path.exists(path):
            return True
        sleep(POLL)
    return not os.path.exists(path)
