"""Hosts a command (e.g. an agent runtime's interactive CLI) in a detached tmux session an agent or a person can watch
and drive; the new core's only tmux caller. One file, tmux 3.3+ and the stdlib (3.9+), importing no core module: copy it
anywhere. start never opens a pane; show does. Errors raise TuiError.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from typing import NamedTuple

NAME = re.compile(r"[A-Za-z0-9_-]+")
PANE = re.compile(r"%[0-9]+")
ITERM_ID = re.compile(r"[A-Za-z0-9-]+")
ITERM_OPENER = re.compile(rf"[A-Za-z0-9]+:{ITERM_ID.pattern}")   # w0t0p0:<unique id>
OPENER = re.compile(rf"{NAME.pattern}|{ITERM_OPENER.pattern}")
SESSION_ID = re.compile(r"\$[0-9]+")
USER_OPTION = re.compile(r"@[A-Za-z0-9_-]+")
# a missing session or server (a stale socket: refused) or socket
ABSENT = re.compile(r"can't find session.*|no server running.*|error connecting to .* \(No such file or directory\)",
                    re.S)
RUNNING = "running"
UNKNOWN = "unknown"   # tmux failed otherwise, or is missing
HANDOVER_TIMEOUT = 10
POLL = 0.1
RESPAWN_WAIT = 10
PAUSE = 0.5
SHOW_TIMEOUT = 30
MARK_TIMEOUT = 5
PLACEHOLDERS = {"session"}
SLOT = re.compile(r"\{\{([^{}]*)\}\}")
SPLITS = ("right", "below")
DIED = "set-option @state dead"
# q: shell-quotes the name and @events when the hook fires, so neither runs as code
DEAD_EVENT = "run-shell -b 'echo \"$(date +%H:%M:%S)\" #{q:session_name} dead >> #{q:@events}'"
BORDER = " #{session_name} #{@state} "
CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{client_control_mode} #{socket_path}"
PANES = "#{pane_dead} #{pane_tty} #{pane_id}"
SESSIONS = "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}"
WINDOW = re.compile(r"@[0-9]+")
PER_COLUMN = 3
GRID_MANAGER = "@grid-manager"
GRID_PER_COLUMN = "@grid-per-column"
GRID_TILES = "@grid-tiles"
GRID_BUSY = "@grid-busy"
GRID_PANES_SET = "@grid-panes"   # sorted pane ids at the last tile
PANE_SET = re.compile(r"%[0-9]+( %[0-9]+)*")
GRID_HOOKS = ("pane-exited", "window-resized", "window-layout-changed")
RETILES = ("all", "open-close", "off")
RETILE = "open-close"
# open-close passes only on a new pane set: tmux 3.4 runs pane-exited only while the window's session is shown
RETILE_HOOKS = {"all": GRID_HOOKS, "open-close": ("pane-exited", "window-layout-changed"), "off": ()}
GRID_WINDOW = ("#{window_width}\t#{window_height}\t#{window_zoomed_flag}\t#{window_layout}\t"
               f"#{{{GRID_MANAGER}}}\t#{{{GRID_PER_COLUMN}}}\t#{{{GRID_PANES_SET}}}\t#{{{GRID_TILES}}}")
GRID_PANES = f"#{{window_id}}\t#{{{GRID_MANAGER}}}\t#{{pane_id}}\t#{{pane_width}}\t#{{pane_height}}\t#{{socket_path}}"
GRID_ROW = re.compile(r"(@[0-9]+)\t([^\t]*)\t(%[0-9]+)\t([0-9]+)\t([0-9]+)\t([^\t]+)")
HOOK_PATH = re.compile(r"[A-Za-z0-9_./@+-]+")
TILE_WAIT = 5
TILE_POLL = 0.05
TILE_LIMIT = 50   # re-tiles per TILE_WINDOW s; one more stops auto re-tiling
TILE_WINDOW = 10
SETTLE_ITERATIONS = 3
LEAF, LEFT_RIGHT, TOP_BOTTOM = "", "{}", "[]"
LAYOUT_CELL = re.compile(r"([0-9]+)x([0-9]+),([0-9]+),([0-9]+)(?:,([0-9]+)|([{[]))")
NO_PANE = "no anchor pane: {}, no iTerm2 pane ($ITERM_SESSION_ID)"
# -a: ancestors too (the caller usually runs inside iTerm2)
PGREP = ["pgrep", "-a", "-x", "iTerm2"]
# argv: split, anchor kind (id or tty), tmux path, session, anchors; never script text
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
                  tell s to set newSession to split vertically with default profile command paneCommand
                else
                  tell s to set newSession to split horizontally with default profile command paneCommand
                end if
                return "ok " & (unique id of newSession)
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
# Runs as `python -I -c EXEC <file>`, so no '#' or trailing ';'. Resets SIGPIPE and SIGXFSZ as Popen does; `block`
# stays blocked across execve; a cwd it cannot enter leaves the handover untaken; it waits up to RESPAWN_WAIT s for
# `after` to exit or turn zombie.
EXEC = f"""import json, os, signal, sys, time
with open(sys.argv[1]) as f: h = json.load(f)
signal.pthread_sigmask(signal.SIG_BLOCK, h.get('block', []))
os.chdir(h['cwd'])
os.unlink(sys.argv[1])
for _ in range({round(RESPAWN_WAIT / POLL)} if h.get('after') else 0):
    try: os.kill(h['after'], 0)
    except OSError: break
    try:
        with open('/proc/%d/stat' % h['after'], 'rb') as f: z = f.read().rsplit(b')', 1)[-1].split()[:1] == [b'Z']
    except OSError: z = False
    if z: break
    time.sleep({POLL})
h['env'].update((k, os.environ[k]) for k in {TERMINAL_KEYS!r} if k in os.environ)
for s in (signal.SIGPIPE, signal.SIGXFSZ): signal.signal(s, signal.SIG_DFL)
os.execve(h['argv'][0], h['argv'], h['env'])"""


class TuiError(Exception):
    pass


class Anchor(NamedTuple):
    """The pane a show goes beside: iterm (("tty", ttys) or ("id", [unique id]); None in tmux), else `pane` on
    `socket`."""
    iterm: tuple[str, list[str]] | None
    pane: str | None = None
    socket: str | None = None
    session: str | None = None


class Cell(NamedTuple):
    """A layout cell: w x h at x, y; a LEAF shows pane, a container holds children."""
    w: int
    h: int
    x: int
    y: int
    pane: str | None = None
    kind: str = LEAF
    children: tuple[Cell, ...] = ()


class Slot(NamedTuple):
    """A grid cell: a pane and the slots stacked right of it, or (pane None) an orphan group."""
    pane: str | None
    children: tuple[Slot, ...] = ()


def start(session: str, argv: list[str], *, cwd: str, env: dict[str, str], events: str | None = None,
          status_line: bool = False, tags: dict[str, str] | None = None, proc=subprocess.run, sleep=time.sleep) -> None:
    """Runs argv in a new detached session that keeps its pane once argv ends, in cwd with env (its terminal keys the
    pane's, PWD cwd), tags, @events, the pane-died hook (@state dead, a dead line to @events), the `<session> <state>`
    border and the status line set in the same tmux call (an instant death still gets its dead line); opens no pane.
    A running session is refused, a dead one replaced; argv reaches the pane through a 0600 handover file. Raising, it
    leaves no session of its own."""
    _name(session)
    options = _tags(session, tags or {})
    argv = _command(argv, env)
    if events is not None:
        events = events_file(events)
    tmp = None
    may_run = False
    try:
        tmp = tempfile.mkdtemp()
        path, wrapper = _wrapper(tmp)
        state = status(session, proc=proc)
        if state == RUNNING:
            raise TuiError(f"session {session} is running; {attach_command(session)}")
        if isinstance(state, int):
            kill(session, proc=proc)
        _handover(path, argv, cwd, env)
        cols, rows = shutil.get_terminal_size()
        decorations = [arg for args in _decorations(session, events, status_line) for arg in (";", *args)]
        # new-session may have run even when the tmux call fails
        may_run = True
        res = _tmux(["new-session", "-d", "-s", session, "-x", str(cols), "-y", str(rows), *wrapper,
                     ";", "set-option", "-p", "-t", f"={session}:", "remain-on-exit", "on", *options, *decorations],
                    proc)
        if res.returncode:
            if _err(res).startswith("duplicate session:"):   # another start took the name: not ours to kill
                may_run = False
            raise TuiError(f"tmux: {_err(res)}")
        if not _taken(path, sleep):
            raise TuiError("the session did not start")
        may_run = False
    except OSError as e:
        raise TuiError(f"handover: {e}") from e
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)   # first, so a late wrapper finds no file to run
        if may_run:
            with contextlib.suppress(TuiError):
                kill(session, proc=proc)


def detach(session: str, argv: list[str], *, cwd: str, env: dict[str, str], block=(), opener: str | None = None,
           proc=subprocess.run, sleep=time.sleep) -> None:
    """Runs argv as start does, `block` blocked until argv unblocks them; env's full iTerm2 $ITERM_SESSION_ID stays and
    opener becomes @opener. No decoration, no kept pane. An existing session is refused untouched."""
    _name(session)
    if opener is not None and not OPENER.fullmatch(opener):
        raise TuiError(f"invalid opener {opener!r}: want a tmux session name or $ITERM_SESSION_ID's value")
    for arg in argv:   # execve would fail after the handover is taken
        if "\0" in arg:
            raise TuiError("an argv item holds a NUL character")
        try:
            os.fsencode(arg)
        except UnicodeEncodeError:
            raise TuiError("an argv item cannot be encoded") from None
    argv = _command(argv, env)
    iterm = env.get("ITERM_SESSION_ID", "")
    terminal = ["-e", f"ITERM_SESSION_ID={iterm}"] if ITERM_OPENER.fullmatch(iterm) else []
    tmp = None
    may_run = False
    try:
        tmp = tempfile.mkdtemp()
        path, wrapper = _wrapper(tmp)
        _handover(path, argv, cwd, env, block=[int(s) for s in block])
        may_run = True
        res = _tmux(["new-session", "-d", *terminal, "-s", session, *wrapper,
                     *([";", "set-option", "-t", f"={session}:", "@opener", opener] if opener is not None else [])],
                    proc)
        if res.returncode:
            if _err(res).startswith("duplicate session:"):
                may_run = False
                raise TuiError(f"session {session} exists")
            raise TuiError(f"tmux: {_err(res)}")
        if not _taken(path, sleep):
            raise TuiError(f"session {session} did not start")
        may_run = False
    except OSError as e:
        raise TuiError(f"handover: {e}") from e
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)   # first, so a late wrapper finds no file to run
        if may_run:
            with contextlib.suppress(TuiError):
                kill(session, proc=proc)


def respawn(session: str, argv: list[str], *, cwd: str, env: dict[str, str], proc=subprocess.run,
            sleep=time.sleep) -> None:
    """Runs argv in the session's pane in place of its command (respawn-pane -k), clearing its history in the same
    command; argv starts once the old process is gone, or after RESPAWN_WAIT s."""
    _name(session)
    argv = _command(argv, env)
    tmp = None
    try:
        tmp = tempfile.mkdtemp()
        path, wrapper = _wrapper(tmp)
        target = f"={session}:"
        pid = _tmux_ok(["display-message", "-p", "-t", target, "#{pane_pid}"], proc).stdout.strip()
        _handover(path, argv, cwd, env, after=int(pid) if re.fullmatch(r"[1-9][0-9]*", pid) else None)
        _tmux_ok(["clear-history", "-t", target, ";", "respawn-pane", "-k", "-t", target, *wrapper], proc)
        if not _taken(path, sleep):
            raise TuiError("the pane did not respawn")
    except OSError as e:
        raise TuiError(f"handover: {e}") from e
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _command(argv: list[str], env: dict[str, str]) -> list[str]:
    if not argv:
        raise TuiError("no command")
    exe = shutil.which(argv[0], path=env.get("PATH", os.defpath))
    if exe is None:
        raise TuiError(f"command not found: {argv[0]}")
    return [os.path.abspath(exe), *argv[1:]]


def _wrapper(tmp: str) -> tuple[str, list[str]]:
    path = os.path.join(tmp, "handover.json")
    for arg in (sys.executable, path):
        if "#" in arg or arg.endswith(";"):
            raise TuiError(f"tmux would misread {arg!r}")
    return path, [sys.executable, "-I", "-c", EXEC, path]


def _handover(path: str, argv: list[str], cwd: str, env: dict[str, str], after: int | None = None,
              block: list[int] | None = None) -> None:
    cwd = os.path.abspath(cwd)
    child = {k: v for k, v in env.items() if k not in TERMINAL_KEYS}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump({"argv": argv, "cwd": cwd, "env": {**child, "PWD": cwd}, **({"after": after} if after else {}),
                   **({"block": block} if block is not None else {})}, f)


def _tags(session: str, tags: dict[str, str]) -> list[str]:
    args = []
    for key, value in tags.items():
        if not USER_OPTION.fullmatch(key) or not isinstance(value, str) or value.endswith(";"):
            raise TuiError(f"tag {key!r}={value!r}: want {USER_OPTION.pattern} and a str tmux takes verbatim")
        args += [";", "set-option", "-t", f"={session}:", key, value]
    return args


def _taken(path: str, sleep) -> bool:
    for _ in range(round(HANDOVER_TIMEOUT / POLL)):
        if not os.path.exists(path):
            return True
        sleep(POLL)
    return not os.path.exists(path)


def mark(word: str, events: str | None, *, pane: str | None = None, tmux: str = "tmux",
         proc=subprocess.run) -> None:
    """Hook protocol: sets the pane ($TMUX_PANE when None) @state to word; done and blocked also append
    `HH:MM:SS <session> <word>` to events, an existing regular file (None: no line)."""
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
            os.write(fd, f"{time.strftime('%H:%M:%S')} {session} {word}\n".encode())
        finally:
            os.close(fd)
    except OSError as e:
        raise TuiError(f"events file {events}: {e.strerror}") from e


def _mark_tmux(argv: list[str], proc) -> str:
    try:
        res = proc(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=MARK_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise TuiError(f"tmux: timed out after {MARK_TIMEOUT} s") from None
    except OSError as e:
        raise TuiError(f"tmux: {e}") from e
    if res.returncode:
        raise TuiError(f"tmux: {_err(res)}")
    return res.stdout


def events_file(path: str) -> str:
    """The absolute path, created 0600 when missing; TuiError unless a regular file tmux takes verbatim."""
    path = os.path.abspath(path)
    if "#" in path or path.endswith(";"):
        raise TuiError(f"events file {path}: tmux would misread it")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise TuiError(f"events file {path}: {e.strerror}") from e
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise TuiError(f"events file {path}: not a regular file owned by you")
    return path


def _decorations(session: str, events: str | None, status_line: bool) -> list[list[str]]:
    target = f"={session}:"
    died = DIED if events is None else f"{DIED} ; {DEAD_EVENT}"
    return [*([["set-option", "-t", target, "@events", events]] if events is not None else []),
            ["set-hook", "-p", "-t", target, "pane-died", died],
            ["set-option", "-w", "-t", target, "pane-border-status", "top"],
            ["set-option", "-w", "-t", target, "pane-border-format", BORDER],
            ["set-option", "-t", target, "status", "on" if status_line else "off"]]


def status(session: str, *, proc=subprocess.run):
    """RUNNING, None (no session or server), UNKNOWN, or the dead pane's exit status (signal n: -n)."""
    target = f"={_name(session)}:"
    try:
        res = _tmux(["display-message", "-p", "-t", target, "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"],
                    proc)
    except TuiError:
        return UNKNOWN
    if res.returncode:
        return None if _absent(res) else UNKNOWN
    fields = res.stdout.rstrip("\n").split(" ")
    if fields == ["", "", ""]:   # tmux (3.7) exits 0 with empty fields for a missing session
        return None
    if len(fields) != 3 or fields[0] not in ("0", "1"):
        return UNKNOWN
    dead, code, sig = fields
    if dead == "0" or not (code or sig):   # dead, but not yet reaped
        return RUNNING
    if sig:   # libc's sys_signame (macOS: "kill"), else the number
        return -(int(sig) if sig.isdigit() else getattr(signal, "SIG" + sig.upper(), 0))
    return int(code)


def sessions(prefix: str = "", *, proc=subprocess.run) -> list[str] | None:
    """Names starting with prefix, sorted; [] when no server runs, None when tmux fails."""
    try:
        res = _tmux(["list-sessions", "-F", "#{session_name}"], proc)
    except TuiError:
        return None
    if res.returncode:
        return [] if _absent(res) else None
    return sorted(n for n in res.stdout.split("\n") if NAME.fullmatch(n) and n.startswith(prefix))


def kill(session: str, *, proc=subprocess.run) -> None:
    _tmux_ok(["kill-session", "-t", f"={_name(session)}"], proc)


def send(session: str, text: str, *, proc=subprocess.run, sleep=time.sleep) -> None:
    paste(session, text, proc=proc)
    sleep(PAUSE)
    keys(session, "Enter", proc=proc)


def paste(session: str, text: str, *, proc=subprocess.run) -> None:
    """A bracketed paste through a buffer of its own, deleted even on failure."""
    target = f"={_name(session)}:"
    buffer = f"tui-{session}-{uuid.uuid4().hex}"
    # load-buffer from stdin keeps text exact: send-keys -l lost long text's head, set-buffer a trailing ';'
    _tmux_ok(["load-buffer", "-b", buffer, "-"], proc, text)
    try:
        _tmux_ok(["paste-buffer", "-p", "-d", "-b", buffer, "-t", target], proc)
    except TuiError:
        with contextlib.suppress(TuiError):
            _tmux(["delete-buffer", "-b", buffer], proc)
        raise


def keys(session: str, *names: str, proc=subprocess.run) -> None:
    """Presses tmux keys (Enter, Escape, C-c); a name tmux would misread raises first."""
    target = f"={_name(session)}:"
    for name in names:
        if name.startswith("-") or name.endswith(";"):
            raise TuiError(f"tmux would misread {name!r}")
    _tmux_ok(["send-keys", "-t", target, *names], proc)


def option(session: str, name: str, *, proc=subprocess.run) -> str | None:
    """None when unset or the session absent."""
    target = f"={_name(session)}:"
    if not USER_OPTION.fullmatch(name):
        raise TuiError(f"option {name!r}: want {USER_OPTION.pattern}")
    res = _tmux(["show-options", "-v", "-t", target, name], proc)
    if res.returncode:
        if _err(res).startswith("invalid option:") or _absent(res):
            return None
        raise TuiError(f"tmux: {_err(res)}")
    return res.stdout.removesuffix("\n")


def pane(session: str, *, proc=subprocess.run) -> str | None:
    target = f"={_name(session)}:"
    res = _tmux(["display-message", "-p", "-t", target, "#{pane_id}"], proc)
    if res.returncode:
        if _absent(res):
            return None
        raise TuiError(f"tmux: {_err(res)}")
    found = res.stdout.strip()
    if not found:   # tmux (3.7) exits 0 printing nothing for a missing session
        return None
    if not PANE.fullmatch(found):
        raise TuiError(f"bad tmux pane {found!r}")
    return found


def read(session: str, lines: int | None = None, *, proc=subprocess.run) -> str:
    """The visible pane's text, or its last `lines` lines with history; trailing blank lines dropped."""
    args = ["capture-pane", "-p", "-J", "-t", f"={_name(session)}:"]
    if lines is not None:
        if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
            raise TuiError(f"lines must be a positive int, not {lines!r}")
        args += ["-S", f"-{lines}"]
    text = _tmux_ok(args, proc).stdout.splitlines()
    while text and not text[-1].strip():
        text.pop()
    return "\n".join(text if lines is None else text[-lines:])


def attach_command(session: str) -> str:
    """`tmux attach -t '=<session>'`, after $TUI_ATTACH_PREFIX when not blank."""
    prefix = os.environ.get("TUI_ATTACH_PREFIX", "").strip()
    return f"{prefix} tmux attach -t '={session}'" if prefix else f"tmux attach -t '={session}'"


def show(session: str, template: str | None = None, *, split: str | None = None, split_from: str | None = None,
         opener: str | None = None, per_column: int | None = None, retile: str | None = None,
         proc=subprocess.run) -> str | None:
    """Prints how to attach, then runs template, else open_pane; "" runs nothing. A failure is returned, never raised,
    but open_pane's TuiError for a bad argument."""
    print(f"tui: session {session}: {attach_command(session)}", file=sys.stderr)
    if template is None:
        why = open_pane(session, split=split, split_from=split_from, opener=opener, per_column=per_column,
                        retile=retile, proc=proc)
    else:
        why = _run(session, template, proc) if template else None
    if why:
        print(f"tui: show: {why}", file=sys.stderr)
    return why


def open_pane(session: str, *, split: str | None = None, split_from: str | None = None, opener: str | None = None,
              per_column: int | None = None, retile: str | None = None, proc=subprocess.run) -> str | None:
    """Opens a pane on the session, recording @opener and @pane. Grid mode (_grid): splits the pane _grid picks and
    tiles the window per retile and per_column, ignoring split and split_from. Else below the newest open @pane of the
    opener's other sessions, else right of the opener's pane; or beside split_from's pane on side split. None or the
    failure."""
    _layout(split, split_from, opener)
    _per_column(per_column)
    _retile(retile)
    tmux = shutil.which("tmux")
    if tmux is None:
        return "tmux not found"
    tmux = os.path.abspath(tmux)
    own = grid = None
    try:
        if opener is None:
            try:
                opener, own = _opener(proc), True
            except TuiError:
                if split_from is None:
                    raise
        in_tmux = opener is not None and ":" not in opener
        sessions = _sessions(proc) if in_tmux or (split is None and split_from is None) else []
        grid = _grid(opener, own, sessions, proc) if in_tmux else None
        if grid is not None:
            window, manager, at, side = grid
            if split is not None or split_from is not None:
                print("tui: show: grid: --split/--split-from ignored", file=sys.stderr)
            new, why = _split(session, side, at, tmux, proc)
        elif split_from is not None:
            new, why = _split(session, split or "right", anchor(split_from, proc=proc), tmux, proc)
        elif split is not None:
            new, why = _split(session, split, _opener_pane(opener, own, proc), tmux, proc)
        else:
            new, why = (_stack(session, opener, sessions, tmux, proc)
                        or _split(session, "right", _opener_pane(opener, own, proc), tmux, proc))
    except TuiError as e:
        return f"{e}; watch it with {attach_command(session)}"
    if new is None:
        return why
    _record(session, opener, new, proc)
    if grid is None:
        return None
    return _grid_up(window, manager, PER_COLUMN if per_column is None else per_column,
                    RETILE if retile is None else retile, proc)


def _sessions(proc) -> list[list[str]]:
    res = _tmux(["list-sessions", "-F", SESSIONS], proc)
    lines = res.stdout.split("\n") if res.returncode == 0 else []
    return [f for f in (line.split("\t") for line in lines) if len(f) == 5 and SESSION_ID.fullmatch(f[0])]


def _grid(opener: str, own: bool | None, sessions: list[list[str]], proc) -> tuple[str, str, Anchor, str] | None:
    """(grid window, manager, pane to split, side) when the opener is in a grid or a root a client shows; None: iTerm2
    mode. It splits the largest pane but the manager below, else the manager right."""
    row = next((f for f in sessions if f[1] == opener), None)
    if row is None:
        return None
    try:
        found = _window(row[3], None, proc) if PANE.fullmatch(row[3]) else None
        if found is None or found[1] is None:
            clients = [] if OPENER.fullmatch(row[2]) else _clients(opener, proc)
            if not clients:
                return None
            if own is None:
                own = own_session(proc=proc) == opener
            pane = os.environ["TMUX_PANE"] if own else clients[0][2]
            found = _window(pane, pane, proc)
    except TuiError:
        return None
    if found is None:
        return None
    window, manager, areas, socket = found
    workers = [p for p in areas if p != manager]
    if not workers:
        return window, manager, Anchor(None, manager, socket), "right"
    return window, manager, Anchor(None, max(workers, key=lambda p: (areas[p], int(p[1:]))), socket), "below"


def _window(pane: str, manager: str | None, proc) -> tuple[str, str | None, dict[str, int], str] | None:
    """pane's window id, manager, pane areas and socket; None when tmux fails."""
    res = _tmux(["list-panes", "-t", pane, "-F", GRID_PANES], proc)
    rows = [GRID_ROW.fullmatch(line) for line in res.stdout.splitlines()] if res.returncode == 0 else []
    if not rows or not all(rows):
        return None
    areas = {m[3]: int(m[4]) * int(m[5]) for m in rows}
    window, stored = rows[0].group(1, 2)
    return window, stored if stored in areas else manager, areas, rows[0][6]


def _grid_up(window: str, manager: str, per_column: int, retile: str, proc) -> str | None:
    """Sets the grid options and retile's hooks (none when tmux would misread a path), then tiles unless off. None or
    the failure."""
    args = ["set-option", "-w", "-t", window, GRID_MANAGER, manager, ";",
            "set-option", "-w", "-t", window, GRID_PER_COLUMN, str(per_column), ";",
            "set-option", "-u", "-w", "-t", window, GRID_BUSY]
    mode = RETILE_HOOKS[retile]
    paths = (sys.executable or "", os.path.abspath(__file__))
    bad = [p for p in paths if not HOOK_PATH.fullmatch(p)] if mode else []
    if bad:
        print(f"tui: show: grid: hooks: {bad[0]}: tmux would misread it", file=sys.stderr)
    flags = "--no-wait --if-panes-changed" if retile == "open-close" else "--no-wait"
    hook = (f"if-shell -F '#{{==:#{{{GRID_BUSY}}},{window}}}' '' "
            f"\"run-shell -b '{paths[0]} -I {paths[1]} tile {flags} {window} >/dev/null 2>&1 || true'\"")
    for name in GRID_HOOKS:
        if name not in mode:
            args += [";", "set-hook", "-u", "-w", "-t", window, name]
        elif not bad:
            args += [";", "set-hook", "-w", "-t", window, name, hook]
    try:
        _tmux_ok(args, proc)
        if mode:
            tile(window, per_column=per_column, proc=proc)
    except TuiError as e:
        return str(e)
    return None


def _grid_down(window: str, proc) -> None:
    options = (GRID_MANAGER, GRID_PER_COLUMN, GRID_TILES, GRID_BUSY, GRID_PANES_SET)
    args = [a for command, names in (("set-hook", GRID_HOOKS), ("set-option", options)) for name in names
            for a in (";", command, "-u", "-w", "-t", window, name)]
    _tmux_ok(args[1:], proc)


def _stack(session: str, opener: str, sessions: list[list[str]], tmux: str,
           proc) -> tuple[str | None, str | None] | None:
    """None when no @pane of the opener's other sessions is open."""
    rows = [(int(f[0][1:]), f[3], f[4]) for f in sessions
            if f[1] != session and f[2] == opener and (PANE.fullmatch(f[3]) or ITERM_ID.fullmatch(f[3]))]
    live = None
    for is_tmux, group in itertools.groupby(sorted(rows, reverse=True), key=lambda r: bool(PANE.fullmatch(r[1]))):
        group = list(group)
        if not is_tmux:   # open iff the AppleScript finds it: one try covers them, newest first
            with contextlib.suppress(TuiError):
                return _split(session, "below", Anchor(("id", [r[1] for r in group])), tmux, proc)
            continue
        if live is None:
            res = _tmux(["list-panes", "-a", "-F", "#{pane_id}"], proc)
            live = set(res.stdout.split("\n")) if res.returncode == 0 else set()
        for _, pane, socket in group:
            if pane in live:
                return _split(session, "below", Anchor(None, pane, socket), tmux, proc)
    return None


def _split(session: str, side: str, a: Anchor, tmux: str, proc) -> tuple[str | None, str | None]:
    """(new pane, None) or (None, tmux's failure), in iTerm2 when one shows a's pane; TuiError with no pane."""
    if a.iterm:
        try:
            return _iterm(session, side, tmux, a.iterm, proc), None
        except TuiError:
            if a.pane is None:
                raise
    command = ["env", "-u", "TMUX", tmux, "-S", a.socket, "attach", "-t", f"={session}"]   # argv: as a sh -c string it died at once (tmux 3.7)
    for arg in command:
        if "#" in arg or arg.endswith(";"):
            raise TuiError(f"tmux would misread {arg!r}")
    try:
        res = _tmux(["split-window", "-d", "-h" if side == "right" else "-v", "-P", "-F", "#{pane_id}", "-t", a.pane,
                     *command], proc)
    except TuiError as e:
        return None, str(e)
    return (None, f"tmux: {_err(res)}") if res.returncode else (res.stdout.strip(), None)


def _iterm(session: str, split: str, tmux: str, iterm: tuple[str, list[str]], proc) -> str:
    try:
        running = proc(PGREP, capture_output=True, text=True, stdin=subprocess.DEVNULL).returncode == 0
    except OSError:
        running = False
    if not running:
        raise TuiError("iTerm2 is not running")
    kind, anchors = iterm
    try:
        res = proc(["osascript", "-e", APPLESCRIPT, split, kind, tmux, session, *anchors],
                   capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=SHOW_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise TuiError(f"osascript timed out after {SHOW_TIMEOUT} s") from None
    except OSError as e:
        raise TuiError(f"osascript: {e}") from e
    if res.returncode:
        raise TuiError(f"osascript: {_err(res)}")
    out = res.stdout.strip()
    if out.startswith("ok ") and ITERM_ID.fullmatch(out[3:]):
        return out[3:]
    raise TuiError(out or "osascript printed nothing")


def _record(session: str, opener: str | None, pane: str, proc) -> None:
    """For the next placement; a failure is only printed."""
    if not (PANE.fullmatch(pane) or ITERM_ID.fullmatch(pane)):
        print(f"tui: show: @pane: not a pane id: {pane!r}", file=sys.stderr)
        return
    for key, value in (("@opener", opener), ("@pane", pane)):
        if value is None:
            continue
        try:
            _tmux_ok(["set-option", "-t", f"={session}:", key, value], proc)
        except TuiError as e:
            print(f"tui: show: {key}: {e}", file=sys.stderr)
            return


def tile(window: str, *, per_column: int | None = None, wait: bool = True, if_panes_changed: bool = False,
         proc=subprocess.run) -> None:
    """Lays out grid window `window` under its _grid_lock (no wait: nothing while another tile holds it), its hooks
    muted by @grid-busy. if_panes_changed: no pass on an unchanged pane set. Over TILE_LIMIT re-tiles in TILE_WINDOW s,
    the grid is torn down."""
    if not WINDOW.fullmatch(window):
        raise TuiError(f"invalid window id {window!r}: want {WINDOW.pattern}")
    _per_column(per_column)
    socket = _tmux_ok(["display-message", "-p", "-t", window, "#{socket_path}"], proc).stdout.rstrip("\n")
    with _grid_lock(socket, window, wait) as held:
        found = _grid_pass(window, per_column, if_panes_changed, proc) if held else None
        if found is None:
            return
        swaps, layout, tiles, panes = found
        if layout is None:
            _tmux_ok(["set-option", "-w", "-t", window, GRID_PANES_SET, panes], proc)
            return
        now = int(time.monotonic() * 1000)
        times = [t for t in (int(s) for s in tiles.split() if re.fullmatch(r"[0-9]{1,15}", s))
                 if now - TILE_WINDOW * 1000 < t <= now] + [now]
        if len(times) > TILE_LIMIT:
            _grid_down(window, proc)
            line = (f"tui: grid {window}: auto re-tiling stopped: {len(times)} re-tiles in {TILE_WINDOW} s; the next "
                    "worker that opens rebuilds it")
            with contextlib.suppress(TuiError):
                _tmux(["display-message", "-d", "10000", "-t", window, line], proc)
            print(line, file=sys.stderr)
            return
        try:
            _tmux_ok(["set-option", "-w", "-t", window, GRID_TILES, " ".join(map(str, times)), ";",
                      "set-option", "-w", "-t", window, GRID_PANES_SET, panes, ";",
                      "set-option", "-w", "-t", window, GRID_BUSY, window, ";",
                      *(a for source, target in swaps for a in ("swap-pane", "-d", "-s", source, "-t", target, ";")),
                      "select-layout", "-t", window, _render_layout(layout)], proc)
        finally:
            with contextlib.suppress(TuiError):
                _tmux(["set-option", "-u", "-w", "-t", window, GRID_BUSY], proc)


def _grid_pass(window: str, per_column: int | None, if_panes_changed: bool,
               proc) -> tuple[list[tuple[str, str]], Cell | None, str, str] | None:
    """tile's pass: (swaps, layout, @grid-tiles, pane set) or None (zoomed, no grid, unreadable layout, unchanged set).
    Up to SETTLE_ITERATIONS more passes run while the result changes: the hooks are muted."""
    fields = _tmux_ok(["display-message", "-p", "-t", window, GRID_WINDOW], proc).stdout.rstrip("\n").split("\t", 7)
    if len(fields) != 8 or fields[2] != "0" or not fields[4]:
        return None
    width, height, _, layout, manager, n, recorded, tiles = fields
    panes = _tmux_ok(["list-panes", "-t", window, "-F", "#{pane_id}"], proc).stdout.split()
    current = _parse_layout(layout)
    leaves = [] if current is None else _leaves(current)
    if current is None or sorted(leaf.pane for leaf in leaves) != sorted(panes):
        return None
    if manager not in panes or len(panes) == 1:
        _grid_down(window, proc)
        return None
    pane_set = " ".join(sorted(panes, key=lambda p: int(p[1:])))
    recorded = recorded if PANE_SET.fullmatch(recorded) else ""
    if if_panes_changed and recorded == pane_set:
        return None
    sessions = []
    for f in (line.split("\t") for line in _tmux_ok(["list-sessions", "-F", SESSIONS], proc).stdout.split("\n")):
        if len(f) == 5 and SESSION_ID.fullmatch(f[0]) and NAME.fullmatch(f[1]):
            sessions.append((int(f[0][1:]), f[1], f[2] if OPENER.fullmatch(f[2]) else "",
                             f[3] if PANE.fullmatch(f[3]) else ""))
    if per_column is None:
        per_column = int(n) if re.fullmatch(r"[1-9][0-9]{0,3}", n) else PER_COLUMN
    order, at = panes, current
    for _ in range(1 + SETTLE_ITERATIONS):
        want = _grid_layout(_grid_tree(manager, order, sessions, at), width=int(width), height=int(height),
                            manager=manager, manager_width=next(c.w for c in _leaves(at) if c.pane == manager),
                            per_column=per_column)
        if want is None or _leaves(want) == _leaves(at):
            break
        order, at = [c.pane for c in _leaves(want)], want
    if _leaves(at) == leaves:
        return None if recorded == pane_set else ([], None, tiles, pane_set)
    return _swaps(panes, order), at, tiles, pane_set


@contextlib.contextmanager
def _grid_lock(socket: str, window: str, wait: bool):
    """Yields whether it holds the window's flock (<socket>-<n>.lock); wait: retries every TILE_POLL s, TuiError after
    TILE_WAIT s."""
    if not os.path.isabs(socket):
        raise TuiError(f"grid {window}: socket path {socket!r}: want an absolute path")
    path = f"{socket}-{window[1:]}.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    except OSError as e:
        raise TuiError(f"grid lock {path}: {e.strerror}") from e
    try:
        end = time.monotonic() + TILE_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
            except BlockingIOError:
                held = False
            except OSError as e:
                raise TuiError(f"grid lock {path}: {e.strerror}") from e
            if held or not wait:
                break
            if time.monotonic() >= end:
                raise TuiError(f"grid {window}: busy")
            time.sleep(TILE_POLL)
        yield held
    finally:
        os.close(fd)


def _checksum(body: str) -> str:
    csum = 0
    for byte in body.encode():
        csum = ((csum >> 1) + ((csum & 1) << 15) + byte) & 0xFFFF
    return f"{csum:04x}"


def _parse_layout(text: str) -> Cell | None:
    """The cell tree of `<checksum>,<body>` or a body; None when malformed."""
    csum, sep, body = text.partition(",")
    if sep and re.fullmatch(r"[0-9a-f]{4}", csum):
        if _checksum(body) != csum:
            return None
        text = body
    try:
        cell, end = _parse_cell(text, 0)
    except ValueError:
        return None
    return cell if end == len(text) else None


def _parse_cell(text: str, at: int) -> tuple[Cell, int]:
    m = LAYOUT_CELL.match(text, at)
    if m is None:
        raise ValueError(at)
    w, h, x, y = (int(v) for v in m.group(1, 2, 3, 4))
    if m.group(5) is not None:
        return Cell(w, h, x, y, f"%{m.group(5)}"), m.end()
    kind = LEFT_RIGHT if m.group(6) == "{" else TOP_BOTTOM
    at, children = m.end(), []
    while True:
        child, at = _parse_cell(text, at)
        children.append(child)
        if text.startswith(kind[1], at):
            return Cell(w, h, x, y, kind=kind, children=tuple(children)), at + 1
        if not text.startswith(",", at):
            raise ValueError(at)
        at += 1


def _render_layout(cell: Cell) -> str:
    body = _body(cell)
    return f"{_checksum(body)},{body}"


def _body(c: Cell) -> str:
    head = f"{c.w}x{c.h},{c.x},{c.y}"
    if c.kind == LEAF:
        return f"{head},{c.pane[1:]}"
    return head + c.kind[0] + ",".join(_body(child) for child in c.children) + c.kind[1]


def _leaves(cell: Cell) -> list[Cell]:
    """Depth first: select-layout's pane order."""
    return [cell] if cell.kind == LEAF else [leaf for child in cell.children for leaf in _leaves(child)]


def _paths(cell: Cell, above: tuple[Cell, ...] = ()) -> dict[str, list[Cell]]:
    if cell.kind == LEAF:
        return {cell.pane: [*above, cell]}
    return {pane: path for child in cell.children for pane, path in _paths(child, (*above, cell)).items()}


def _natural(name: str) -> list[str | int]:
    """Text and number chunks: w2 before w10."""
    return [int(c) if i % 2 else c for i, c in enumerate(re.split(r"([0-9]+)", name))]


def _grid_tree(manager: str, panes: list[str], sessions: list[tuple[int, str, str, str]],
               layout: Cell) -> list[Slot]:
    """The main grid's cells in order: a session's node sits in its @opener's, an orphan group (its @opener gone) where
    the layout shows it, the rest in main; main by _natural name, siblings by pane id, manual panes last."""
    owner = {}
    for _, name, opener, pane in sorted(sessions):
        if pane in panes and pane != manager and pane not in owner:
            owner[pane] = name, opener
    node = {name: pane for pane, (name, _) in owner.items()}
    names = {row[1] for row in sessions}
    parent = {}   # a node's pane, or an orphan group's name -> its parent's; None: main
    for pane, (_, opener) in owner.items():
        if opener in node:
            parent[pane] = node[opener]
        else:
            parent[pane] = opener if opener and opener not in names else None

    def under(key) -> list[str]:
        return [*([key] if key in owner else []), *(p for k, up in parent.items() if up == key for p in under(k))]

    paths = _paths(layout)
    rank = {pane: i for i, pane in enumerate(paths)}
    columns = (layout.children[1] if layout.kind == LEFT_RIGHT and len(layout.children) == 2
               and layout.children[0].pane == manager else layout)
    groups = sorted({up for up in parent.values() if up is not None and up not in owner})
    spans = {q: set(under(q)) for q in groups}
    mains = [p for p in panes if p != manager and parent.get(p) is None]
    for q in groups:
        path = paths[min(spans[q])]
        for pane in spans[q]:
            path = [a for a, b in zip(path, paths[pane]) if a is b]
        if path[-1] is layout or path[-1] is columns:
            parent.update((k, None) for k, up in list(parent.items()) if up == q)
            continue
        parent[q] = None
        for cell in reversed(path):
            head = cell.children[0] if cell.kind == LEFT_RIGHT else None
            if head is None or head.kind != LEAF or head.pane in spans[q]:
                continue
            # R is a pane's own cell ({pane, its children}) only with two children and that pane the one main pane
            if head.pane == manager or cell is columns and (len(cell.children) != 2 or mains != [head.pane]):
                break
            if head.pane in owner:
                parent[q] = head.pane
                break
    reached, todo = set(), [None]
    while todo:
        key = todo.pop()
        found = [k for k, up in parent.items() if up == key and k not in reached]
        reached.update(found)
        todo += found
    parent.update((k, None) for k in list(parent) if k not in reached and parent[k] not in spans)

    def first(key) -> int:
        return min(rank[p] for p in under(key))

    def slots(key) -> list[Slot]:
        if key is None:
            order = sorted((k for k, up in parent.items() if up is None),
                           key=lambda k: _natural(owner[k][0] if k in owner else k))
        else:
            live = sorted((k for k, up in parent.items() if up == key and k in owner), key=lambda p: int(p[1:]))
            order = list(live)
            for q in sorted((k for k, up in parent.items() if up == key and k not in owner), key=first):
                later = next((k for k in live if first(k) > first(q)), None)
                order.insert(len(order) if later is None else order.index(later), q)
        return [Slot(k if k in owner else None, tuple(slots(k))) for k in order]

    manual = sorted((p for p in panes if p != manager and p not in owner), key=lambda p: int(p[1:]))
    return [*slots(None), *(Slot(p) for p in manual)]


def _grid_layout(cells: list[Slot], *, width: int, height: int, manager: str, manager_width: int,
                 per_column: int) -> Cell | None:
    """The manager left at manager_width (narrowed to fit), cells in columns of per_column; None: it does not fit."""
    columns = tuple(Slot(None, tuple(cells[i:i + per_column])) for i in range(0, len(cells), per_column))
    c = len(columns)
    mw = min(manager_width if manager_width != width else (width - 1) // 2,
             width - 1 - (c * max(_min_width(s) for s in cells) + c - 1))
    if mw < 1:
        return None
    root = Cell(width, height, 0, 0, kind=LEFT_RIGHT, children=(
        Cell(mw, height, 0, 0, manager), _line(columns, LEFT_RIGHT, width - mw - 1, height, mw + 1, 0)))
    return root if all(leaf.h >= 1 for leaf in _leaves(root)) else None


def _min_width(s: Slot) -> int:
    if not s.children:
        return 1
    least = max(_min_width(child) for child in s.children)
    return least if s.pane is None else 2 * least + 1


def _place(s: Slot, w: int, h: int, x: int, y: int) -> Cell:
    """A pane takes the left half, its children stack right; an orphan group stacks."""
    if not s.children:
        return Cell(w, h, x, y, s.pane)
    if s.pane is None:
        return _line(s.children, TOP_BOTTOM, w, h, x, y)
    left = w // 2
    return Cell(w, h, x, y, kind=LEFT_RIGHT, children=(
        Cell(left, h, x, y, s.pane), _line(s.children, TOP_BOTTOM, w - left - 1, h, x + left + 1, y)))


def _line(slots: tuple[Slot, ...], kind: str, w: int, h: int, x: int, y: int) -> Cell:
    """Side by side or stacked, equal sizes, the remainder to the first."""
    if len(slots) == 1:
        return _place(slots[0], w, h, x, y)
    across = kind == LEFT_RIGHT
    size, extra = divmod((w if across else h) - len(slots) + 1, len(slots))
    children, at = [], x if across else y
    for i, s in enumerate(slots):
        n = size + (i < extra)
        children.append(_place(s, n, h, at, y) if across else _place(s, w, n, x, at))
        at += n + 1
    return Cell(w, h, x, y, kind=kind, children=tuple(children))


def _swaps(have: list[str], want: list[str]) -> list[tuple[str, str]]:
    """The fewest swap-pane pairs turning index order have into want."""
    have, swaps = list(have), []
    for i, pane in enumerate(want):
        if have[i] != pane:
            j = have.index(pane)
            swaps.append((pane, have[i]))
            have[i], have[j] = pane, have[i]
    return swaps


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tmux_host.py", description="Host a command, e.g. an agent's TUI, in a "
                                 "detached tmux session: start it, "
                                 "type into it, read it, show it. The show: --show T; else, opened from tmux, a grid "
                                 "in the manager's window (it stays left, the panes it opens fill columns of "
                                 "--per-column, top down, then right); else an iTerm2 split, else a tmux split. T may "
                                 "use {{session}}; '' prints only the attach command. "
                                 "$TUI_ATTACH_PREFIX (e.g. 'docker exec -it C') prefixes printed attach commands.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    start_p = sub.add_parser("start", help="run a command in a new detached session (show shows it)",
                             description="session -- command [args...]: everything after the first -- is the "
                                         "command, passed through verbatim.")
    start_p.add_argument("--events", metavar="FILE",
                         help="append `HH:MM:SS <session> done|blocked|dead` lines to FILE (created 0600)")
    start_p.add_argument("session", type=_session_arg)
    p = sub.add_parser("send", help="type text into the session, then Enter")
    p.add_argument("session", type=_session_arg)
    p.add_argument("text")
    p = sub.add_parser("read", help="print the session's pane text")
    p.add_argument("session", type=_session_arg)
    p.add_argument("--lines", type=_lines_arg, metavar="N", help="the last N lines, with history")
    p = sub.add_parser("show", help="show a running session")
    _show_options(p)
    p.add_argument("session", type=_session_arg)
    p = sub.add_parser("tile", help="lay out a grid window's panes (its hooks run this)")
    p.add_argument("--no-wait", action="store_true",
                   help="return at once while another tile lays the window out (the hooks pass it)")
    p.add_argument("--if-panes-changed", action="store_true",
                   help="lay out only when the window's panes are not those of its last tile (open-close's hooks "
                        "pass it: a drag or resize keeps the layout)")
    p.add_argument("window", help="its id, @N")
    argv = sys.argv[1:] if argv is None else list(argv)
    command = None
    if argv[:1] == ["start"] and "--" in argv:   # argparse < 3.13 drops a later -- from a nargs list
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    elif argv[:1] == ["start"] and not {"-h", "--help"} & set(argv):
        start_p.error("a command must follow --")
    a = ap.parse_args(argv)
    if a.cmd == "start" and not command:
        start_p.error("a command must follow --")
    try:
        if a.cmd == "start":
            start(a.session, command, cwd=os.getcwd(), env=dict(os.environ), events=a.events)
        elif a.cmd == "send":
            send(a.session, a.text)
        elif a.cmd == "read":
            print(read(a.session, a.lines))
        elif a.cmd == "tile":
            tile(a.window, wait=not a.no_wait, if_panes_changed=a.if_panes_changed)
        else:
            if status(a.session) is None:
                raise TuiError(f"no session {a.session}")
            return 1 if show(a.session, a.show, split=a.split, split_from=a.split_from, per_column=a.per_column,
                             retile=a.grid_retile) else 0
    except TuiError as e:
        print(f"tui: {e}", file=sys.stderr)
        return 1
    return 0


def _layout(split: str | None, split_from: str | None, opener: str | None = None) -> None:
    if split is not None and split not in SPLITS:
        raise TuiError(f"split must be one of {', '.join(SPLITS)}, not {split!r}")
    if split_from is not None:
        _name(split_from)
    if opener is not None and not OPENER.fullmatch(opener):
        raise TuiError(f"invalid opener {opener!r}: want a tmux session name or $ITERM_SESSION_ID's value")


def _per_column(per_column: int | None) -> None:
    if per_column is not None and (isinstance(per_column, bool) or not isinstance(per_column, int)
                                   or not 1 <= per_column <= 9999):
        raise TuiError(f"per_column must be an int from 1 to 9999, not {per_column!r}")


def _retile(retile: str | None) -> None:
    if retile is not None and retile not in RETILES:
        raise TuiError(f"retile must be one of {', '.join(RETILES)}, not {retile!r}")


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


def own_session(*, proc=subprocess.run) -> str | None:
    """$TMUX_PANE's session; None outside tmux."""
    if not os.environ.get("TMUX"):
        return None
    pane = os.environ.get("TMUX_PANE", "")
    if not PANE.fullmatch(pane):
        raise TuiError(f"bad $TMUX_PANE {pane!r}")
    name = _tmux_ok(["display-message", "-p", "-t", pane, "#{session_name}"], proc).stdout.strip()
    if not NAME.fullmatch(name):
        raise TuiError(f"own tmux session {name!r}: want [A-Za-z0-9_-]+")
    return name


def opener(*, proc=subprocess.run) -> str:
    """The caller's tmux session, else (iTerm.app) its full $ITERM_SESSION_ID; TuiError when neither matches OPENER."""
    return _opener(proc)


def _opener(proc) -> str:
    own = own_session(proc=proc)
    if own is not None:
        return own
    value = os.environ.get("ITERM_SESSION_ID", "")
    if not value or os.environ.get("TERM_PROGRAM") != "iTerm.app":
        raise TuiError(NO_PANE.format("not in tmux"))
    if ":" not in value or not OPENER.fullmatch(value):
        raise TuiError(f"bad $ITERM_SESSION_ID {value!r}: want {OPENER.pattern}")
    return value


def anchor(split_from: str | None = None, *, proc=subprocess.run) -> Anchor:
    """split_from's shown pane, else the caller's session's, else (iTerm.app only) $ITERM_SESSION_ID's; TuiError when
    none."""
    if split_from is not None:
        found = _shown(split_from, False, proc)
        if found is None:
            raise TuiError(f"no terminal shows tmux session {split_from}")
        return found
    own = own_session(proc=proc)
    return _iterm_pane("not in tmux") if own is None else _opener_pane(own, True, proc)


def _opener_pane(opener: str, own: bool | None, proc) -> Anchor:
    if ":" in opener:
        return Anchor(("id", [opener.partition(":")[2]]))
    if own is None:
        own = own_session(proc=proc) == opener
    return _shown(opener, own, proc) or _iterm_pane(f"no terminal shows tmux session {opener}")


def _shown(session: str, own: bool, proc) -> Anchor | None:
    """The pane the most recent client shows the session in: a nested client's host pane, $TMUX_PANE when own, else the
    client's; a control-mode client gets its tmux pane only."""
    clients = _clients(session, proc)
    if not clients:
        return None
    _, tty, pane, control, socket = clients[0]
    pane = os.environ["TMUX_PANE"] if own else pane
    if control == "1":
        return Anchor(None, pane, socket, session)
    hosts = {f[1]: f[2] for f in (line.split(" ", 2) for line in
                                  _tmux_ok(["list-panes", "-a", "-F", PANES], proc).stdout.splitlines())
             if len(f) == 3 and f[0] != "1"}
    if PANE.fullmatch(hosts.get(tty, "")):
        return Anchor(None, hosts[tty], socket, session)
    return Anchor(("tty", [c[1] for c in clients if c[3] == "0"]), pane, socket, session)


def _clients(session: str, proc) -> list[list[str]]:
    out = _tmux_ok(["list-clients", "-t", f"={session}", "-F", CLIENTS], proc).stdout
    return sorted((c for c in (line.split(" ", 4) for line in out.splitlines())
                   if len(c) == 5 and c[0].isdigit() and c[1] and PANE.fullmatch(c[2]) and c[3] in ("0", "1") and c[4]),
                  key=lambda c: int(c[0]), reverse=True)


def _iterm_pane(why: str) -> Anchor:
    unique = os.environ.get("ITERM_SESSION_ID", "").partition(":")[2]
    if not unique or not (os.environ.get("TMUX") or os.environ.get("TERM_PROGRAM") == "iTerm.app"):
        raise TuiError(NO_PANE.format(why))
    return Anchor(("id", [unique]))


def _show_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--show", metavar="T", help="the show command, instead of the split")
    p.add_argument("--split", choices=SPLITS, help="the split's side outside the grid (default: below the newest pane "
                                                    "you opened that still shows, else right of yours; with "
                                                    "--split-from, right)")
    p.add_argument("--split-from", metavar="S", type=_session_arg,
                   help="split the pane showing tmux session S (default: yours), outside the grid")
    p.add_argument("--per-column", metavar="N", type=_per_column_arg, default=PER_COLUMN,
                   help=f"panes per grid column, 1 to 9999 (default {PER_COLUMN})")
    p.add_argument("--grid-retile", metavar="M", choices=RETILES, default=RETILE,
                   help=f"when the grid re-tiles by itself: {RETILE} (default) when a pane opens or closes; all also "
                        "on a drag or a window resize; off never (tile @N)")


def _session_arg(value: str) -> str:
    try:
        return _name(value)
    except TuiError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _per_column_arg(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,4}", value) or int(value) < 1:
        raise argparse.ArgumentTypeError(f"want an int from 1 to 9999, not {value!r}")
    return int(value)


def _lines_arg(value: str) -> int:
    if not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"want a positive int, not {value!r}")
    return int(value)


def _tmux_ok(args: list[str], proc, stdin: str | None = None) -> subprocess.CompletedProcess:
    res = _tmux(args, proc, stdin)
    if res.returncode:
        raise TuiError(f"tmux: {_err(res)}")
    return res


def _name(session: str) -> str:
    if not NAME.fullmatch(session):
        raise TuiError(f"invalid session name {session!r}: want [A-Za-z0-9_-]+")
    return session


def _tmux(args: list[str], proc, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        if stdin is not None:
            return proc(["tmux", *args], capture_output=True, text=True, input=stdin)
        return proc(["tmux", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise TuiError(f"tmux: {e}") from e


def _err(res: subprocess.CompletedProcess) -> str:
    return (res.stderr or "").strip()


def _absent(res: subprocess.CompletedProcess) -> bool:
    return bool(ABSENT.fullmatch(_err(res)))


if __name__ == "__main__":
    sys.exit(main())
