"""Generic tmux host: runs a command in a detached tmux session another agent or a person can watch and drive.

One file, tmux plus the Python stdlib (3.9+): copy it anywhere. CLI: python3 tui.py --help.

start(session, argv, cwd=, env=)  a detached session running argv; the command line, cwd and env reach the pane
                                  through a 0600 handover file, never through tmux; then show(session, show)
status(session)                   None (no such session), RUNNING, or the dead pane's exit status (signal n: 128+n)
send(session, text)               types text into the pane, then Enter
read(session, lines=)             the pane's text: the visible pane, or its last lines with history
show(session, template)           prints how to attach, then runs the template, else $TUI_SHOW, else the iTerm2 split;
                                  returns the failure reason instead of raising
kill(session)                     ends the session
Session names are [A-Za-z0-9_-]+. Errors raise TuiError.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
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
PAUSE = 0.5   # between typing a text and its Enter
SHOW_TIMEOUT = 30
PLACEHOLDERS = {"session"}
SLOT = re.compile(r"\{\{([^{}]*)\}\}")
SPLITS = ("right", "below")
CLIENTS = "#{client_activity} #{client_tty}"
# osascript's argv: split, anchor kind (id: iTerm2 unique ids; tty: client ttys), tmux path, session, anchor values.
# They stay arguments, never script text.
APPLESCRIPT = """on run argv
  set {splitDir, anchorKind, tmuxPath, sessionName} to items 1 thru 4 of argv
  set paneCommand to (quoted form of tmuxPath) & " attach -t " & (quoted form of ("=" & sessionName))
  if application "iTerm2" is running then
    tell application "iTerm2"
      repeat with i from 5 to count of argv
        set anchorValue to item i of argv
        repeat with w in windows
          repeat with t in tabs of w
            repeat with s in sessions of t
              if (anchorKind is "id" and unique id of s is anchorValue) or (anchorKind is "tty" and tty of s is anchorValue) then
                if splitDir is "right" then
                  tell s to split vertically with default profile command paneCommand
                else
                  tell s to split horizontally with default profile command paneCommand
                end if
                return ""
              end if
            end repeat
          end repeat
        end repeat
      end repeat
    end tell
    return "no iTerm2 pane shows the anchor"
  end if
  return "iTerm2 is not running"
end run"""
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
    if _template(show) is None:
        _layout(split, beside)
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
        _tmux_ok(["new-session", "-d", "-s", session, "-x", str(cols), "-y", str(rows),
                  sys.executable, "-I", "-c", EXEC, path,
                  ";", "set-option", "-p", "-t", f"={session}:", "remain-on-exit", "on"], proc)
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
    _tmux_ok(["kill-session", "-t", f"={_name(session)}"], proc)


def send(session: str, text: str, *, proc=subprocess.run, sleep=time.sleep) -> None:
    """Type text into the session's pane, then press Enter."""
    target = f"={_name(session)}:"
    if text.endswith(";"):   # tmux takes an argument ending in ';' for a command separator, and '\;' for ';'
        text = text[:-1] + "\\;"
    _tmux_ok(["send-keys", "-t", target, "-l", "--", text], proc)
    sleep(PAUSE)
    _tmux_ok(["send-keys", "-t", target, "Enter"], proc)


def read(session: str, lines: int | None = None, *, proc=subprocess.run) -> str:
    """The pane's text without trailing blank lines: the visible pane, or its last `lines` lines with history."""
    args = ["capture-pane", "-p", "-J", "-t", f"={_name(session)}:"]
    if lines is not None:
        if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
            raise TuiError(f"lines must be a positive int, not {lines!r}")
        args += ["-S", f"-{lines}"]
    text = _tmux_ok(args, proc).stdout.splitlines()
    while text and not text[-1].strip():
        text.pop()
    return "\n".join(text if lines is None else text[-lines:])


def show(session: str, template: str | None = None, *, split: str = "right", beside: str | None = None,
         proc=subprocess.run) -> str | None:
    """Print how to attach, then run the show: template, else $TUI_SHOW, else the iTerm2 split; "" runs nothing.
    A failure is printed and returned, never raised; only the iTerm2 split raises TuiError, for a bad split or beside."""
    print(f"tui: session {session}: tmux attach -t '={session}'", file=sys.stderr)
    template = _template(template)
    if template is None:
        why = iterm(session, split=split, beside=beside, proc=proc)
    else:
        why = _run(session, template, proc) if template else None
    if why:
        print(f"tui: show: {why}", file=sys.stderr)
    return why


def iterm(session: str, *, split: str = "right", beside: str | None = None, proc=subprocess.run) -> str | None:
    """Split an iTerm2 pane right or below, attached to the session: the pane showing tmux session `beside`, else
    the caller's (inside tmux: the one showing its own session; outside: $ITERM_SESSION_ID's). None or the failure."""
    _layout(split, beside)
    tmux = shutil.which("tmux")
    if tmux is None:
        return "tmux not found"
    try:
        kind, anchors = _anchor(beside, proc)
    except TuiError as e:
        return str(e)
    try:
        res = proc(["osascript", "-e", APPLESCRIPT, split, kind, os.path.abspath(tmux), session, *anchors],
                   capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=SHOW_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"osascript timed out after {SHOW_TIMEOUT} s"
    except OSError as e:
        return f"osascript: {e}"
    if res.returncode:
        return f"osascript: {_err(res)}"
    return res.stdout.strip() or None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tui.py", description="Host a command in a detached tmux session: start it, "
                                 "type into it, read it, show it. The show: --show T, else $TUI_SHOW, else an iTerm2 "
                                 "split; T may use {{session}}; '' prints only the attach command.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    start_p = sub.add_parser("start", help="run a command in a new detached session, then show it")
    _show_options(start_p)
    start_p.add_argument("session", type=_session_arg)
    start_p.add_argument("command", nargs=argparse.REMAINDER, help="[--] cmd [args...]")
    p = sub.add_parser("send", help="type text into the session, then Enter")
    p.add_argument("session", type=_session_arg)
    p.add_argument("text")
    p = sub.add_parser("read", help="print the session's pane text")
    p.add_argument("session", type=_session_arg)
    p.add_argument("--lines", type=_lines_arg, metavar="N", help="the last N lines, with history")
    p = sub.add_parser("show", help="show a running session")
    _show_options(p)
    p.add_argument("session", type=_session_arg)
    a = ap.parse_args(argv)
    if a.cmd == "start":
        command = a.command
        if command[:1] and command[0].startswith("-"):   # REMAINDER also took the options after the session
            if "--" not in command:
                start_p.error("options after the session need -- before the command")
            late = argparse.ArgumentParser(prog=start_p.prog, add_help=False)
            _show_options(late)
            late.parse_args(command[:command.index("--")], namespace=a)
            command = command[command.index("--") + 1:]
        if not command:
            start_p.error("no command")
    try:
        if a.cmd == "start":
            start(a.session, command, cwd=os.getcwd(), env=dict(os.environ), show=a.show, split=a.split,
                  beside=a.beside)
        elif a.cmd == "send":
            send(a.session, a.text)
        elif a.cmd == "read":
            print(read(a.session, a.lines))
        else:
            if status(a.session) is None:
                raise TuiError(f"no session {a.session}")
            return 1 if show(a.session, a.show, split=a.split, beside=a.beside) else 0
    except TuiError as e:
        print(f"tui: {e}", file=sys.stderr)
        return 1
    return 0


def _template(template: str | None) -> str | None:
    return os.environ.get("TUI_SHOW") if template is None else template


def _layout(split: str, beside: str | None) -> None:
    if split not in SPLITS:
        raise TuiError(f"split must be one of {', '.join(SPLITS)}, not {split!r}")
    if beside is not None:
        _name(beside)


def _run(session: str, template: str, proc) -> str | None:
    unknown = [m.group(0) for m in SLOT.finditer(template) if m.group(1) not in PLACEHOLDERS]
    if unknown:
        return f"unknown placeholder {unknown[0]}"
    try:
        res = proc(["/bin/sh", "-c", SLOT.sub(lambda m: shlex.quote(session), template)], stdin=subprocess.DEVNULL,
                   timeout=SHOW_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"timed out after {SHOW_TIMEOUT} s"
    except OSError as e:
        return str(e)
    return f"exit status {res.returncode}" if res.returncode else None


def _anchor(beside: str | None, proc) -> tuple[str, list[str]]:
    """("tty", the ttys of the clients showing a tmux session, most recently active first), or ("id", [unique id])."""
    if beside is None and os.environ.get("TMUX"):
        pane = os.environ.get("TMUX_PANE", "")
        if not re.fullmatch(r"%[0-9]+", pane):
            raise TuiError(f"bad $TMUX_PANE {pane!r}")
        beside = _tmux_ok(["display-message", "-p", "-t", pane, "#{session_name}"], proc).stdout.strip()
        if not NAME.fullmatch(beside):
            raise TuiError(f"own tmux session {beside!r}: want [A-Za-z0-9_-]+")
    if beside is not None:
        out = _tmux_ok(["list-clients", "-t", f"={beside}", "-F", CLIENTS], proc).stdout
        clients = [(int(at), tty) for at, _, tty in (line.partition(" ") for line in out.splitlines())
                   if at.isdigit() and tty]
        ttys = [tty for _, tty in sorted(clients, key=lambda c: c[0], reverse=True)]
        if not ttys:
            raise TuiError(f"no terminal shows tmux session {beside}")
        return "tty", ttys
    unique = os.environ.get("ITERM_SESSION_ID", "").partition(":")[2]
    if not unique:
        raise TuiError("no anchor pane: not in tmux or iTerm2 ($ITERM_SESSION_ID)")
    return "id", [unique]


def _show_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--show", metavar="T", help="the show command; overrides $TUI_SHOW")
    p.add_argument("--split", choices=SPLITS, default="right", help="the iTerm2 split's side (default: right)")
    p.add_argument("--beside", metavar="S", type=_session_arg,
                   help="split the iTerm2 pane showing tmux session S (default: the current pane)")


def _session_arg(value: str) -> str:
    try:
        return _name(value)
    except TuiError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _lines_arg(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"want a positive int, not {value!r}")
    return int(value)


def _tmux_ok(args: list[str], proc) -> subprocess.CompletedProcess:
    res = _tmux(args, proc)
    if res.returncode:
        raise TuiError(f"tmux: {_err(res)}")
    return res


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


if __name__ == "__main__":
    sys.exit(main())
