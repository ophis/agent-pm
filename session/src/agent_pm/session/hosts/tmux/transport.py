"""Validated tmux command transport."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
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


DEAD = "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"


SESSIONS = "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}"


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


class Transport:
    """tmux calls through proc (subprocess.run's signature); the base of Tmux's other parts."""

    def __init__(self, proc=subprocess.run) -> None:
        self.proc = proc

    def checked(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        res = self.run(args, stdin)
        if res.returncode:
            raise TuiError(f"tmux: {error(res)}")
        return res

    def run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        try:
            if stdin is not None:
                return self.proc(["tmux", *args], capture_output=True, text=True, input=stdin)
            return self.proc(
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
