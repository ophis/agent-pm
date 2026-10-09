"""Hosts Claude Code TUIs: runs claude in a detached tmux session another agent or a person can watch and drive.

One file, tmux 3.3+ plus the Python stdlib (3.9+): copy it anywhere. CLI: python3 tui_claude.py --help.
Session names are [A-Za-z0-9_-]+. Errors raise TuiError. Where a session's pane opens: open_pane; tile lays out a tmux
grid window.
"""
from __future__ import annotations

import argparse
import contextlib
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
ITERM_ID = re.compile(r"[A-Za-z0-9-]+")   # an iTerm2 session's unique id
# whose panes are placed together: a tmux session, or an iTerm2 pane by its full $ITERM_SESSION_ID (w0t0p0:<unique id>)
OPENER = re.compile(rf"{NAME.pattern}|[A-Za-z0-9]+:{ITERM_ID.pattern}")
SESSION_ID = re.compile(r"\$[0-9]+")
RUNNING = "running"
HANDOVER_TIMEOUT = 10
POLL = 0.1   # seconds between checks that the wrapper took the handover file
PAUSE = 0.5   # between typing a text and its Enter
SHOW_TIMEOUT = 30
PLACEHOLDERS = {"session"}
SLOT = re.compile(r"\{\{([^{}]*)\}\}")
SPLITS = ("right", "below")
# every notification type but idle_prompt; a matcher with a character outside [A-Za-z0-9_|, -] is an unanchored
# JavaScript regex
MATCHER = "^(?!idle_prompt$)"
DIED = "set-option @state dead"
# tmux expands run-shell's #{...} when the hook fires; q: shell-quotes the name and @events, so neither runs as code.
DEAD_EVENT = "run-shell -b 'echo \"$(date +%H:%M:%S)\" #{q:session_name} dead >> #{q:@events}'"
BORDER = " #{session_name} #{@state} "
CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{client_control_mode} #{socket_path}"
PANES = "#{pane_dead} #{pane_tty} #{pane_id}"
SESSIONS = "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}"
WINDOW = re.compile(r"@[0-9]+")
PER_COLUMN = 3
GRID_MANAGER = "@grid-manager"
GRID_PER_COLUMN = "@grid-per-column"
GRID_HOOKS = ("pane-exited", "window-resized", "window-layout-changed")
GRID_WINDOW = ("#{window_width}\t#{window_height}\t#{window_zoomed_flag}\t#{window_layout}\t"
               f"#{{{GRID_MANAGER}}}\t#{{{GRID_PER_COLUMN}}}")
GRID_PANES = f"#{{window_id}}\t#{{{GRID_MANAGER}}}\t#{{pane_id}}\t#{{pane_width}}\t#{{pane_height}}\t#{{socket_path}}"
GRID_ROW = re.compile(r"(@[0-9]+)\t([^\t]*)\t(%[0-9]+)\t([0-9]+)\t([0-9]+)\t([^\t]+)")
HOOK_PATH = re.compile(r"[A-Za-z0-9_./@+-]+")   # a path tmux and sh take verbatim in a grid hook
LEAF, LEFT_RIGHT, TOP_BOTTOM = "", "{}", "[]"   # a layout cell's kind; a container's are its brackets
LAYOUT_CELL = re.compile(r"([0-9]+)x([0-9]+),([0-9]+),([0-9]+)(?:,([0-9]+)|([{[]))")
NO_PANE = "no anchor pane: {}, no iTerm2 pane ($ITERM_SESSION_ID)"
# before osascript, whose script needs iTerm2 installed; -a includes ancestors (the caller usually runs inside iTerm2)
PGREP = ["pgrep", "-a", "-x", "iTerm2"]
# osascript's argv: split, anchor kind (id: iTerm2 unique ids; tty: client ttys), tmux path, session, anchor values.
# They stay arguments, never script text. It splits the first anchor found; success prints `ok <new unique id>`.
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
# Set in a Claude Code session's commands. A claude inheriting CLAUDE_CODE_CHILD_SESSION saves no transcript and can't
# be resumed; one inheriting CLAUDE_JOB_DIR takes that session's name over --name and writes in its job dir.
PARENT_KEYS = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_JOB_DIR")
# Run in the pane as `python -I -c EXEC <file>`. tmux would misread a '#' or a trailing ';', so it has neither.
# Python ignores SIGPIPE and SIGXFSZ, and execve keeps that: reset them, as Popen does. The handover's `block` signals
# are blocked before the file goes, and execve keeps them blocked: none ends argv before it can handle them.
EXEC = f"""import json, os, signal, sys
with open(sys.argv[1]) as f: h = json.load(f)
signal.pthread_sigmask(signal.SIG_BLOCK, h.get('block', []))
os.unlink(sys.argv[1])
os.chdir(h['cwd'])
h['env'].update((k, os.environ[k]) for k in {TERMINAL_KEYS!r} if k in os.environ)
for s in (signal.SIGPIPE, signal.SIGXFSZ): signal.signal(s, signal.SIG_DFL)
os.execve(h['argv'][0], h['argv'], h['env'])"""


class TuiError(Exception):
    pass


class Anchor(NamedTuple):
    """The pane a show goes beside. iterm: the iTerm2 panes that may show it, ("tty", client ttys but control-mode
    ones, most recently active first) or ("id", [unique id]); None when it is a tmux pane showing a nested client, or
    one of a session a control-mode client shows. pane, socket: the tmux pane split when no iTerm2 pane shows it, and
    its server (None for an "id" anchor). session: the tmux session it was found by."""
    iterm: tuple[str, list[str]] | None
    pane: str | None = None
    socket: str | None = None
    session: str | None = None


class Cell(NamedTuple):
    """A window layout cell as tmux dumps it: w x h at x, y; a LEAF shows pane (%N), a LEFT_RIGHT or TOP_BOTTOM
    container holds children."""
    w: int
    h: int
    x: int
    y: int
    pane: str | None = None
    kind: str = LEAF
    children: tuple[Cell, ...] = ()


class Slot(NamedTuple):
    """A grid cell's content: a pane and the slots stacked right of it, or (pane None) an orphan group's members,
    stacked."""
    pane: str | None
    children: tuple[Slot, ...] = ()


def start(session: str, argv: list[str], *, cwd: str, env: dict[str, str], events: str | None = None,
          template: str | None = None, split: str | None = None, split_from: str | None = None, opener: str | None = None,
          per_column: int | None = None, status_line: bool = False, proc=subprocess.run, sleep=time.sleep) -> None:
    """Run with_hooks(argv, events), argv a claude command, in a new detached session, in cwd with env minus
    PARENT_KEYS, its PWD set to cwd, plus the pane's terminal keys; once it runs, decorate the session (status_line
    too), then show it. events goes through events_file first. argv, cwd and env reach the pane through a 0600 handover
    file, never through tmux. Raising, it leaves no session of its own."""
    _name(session)
    if template is None:
        _layout(split, split_from, opener)
        _per_column(per_column)
    argv = _command(argv, env)
    if events is not None:
        events = events_file(events)
    argv = with_hooks(argv, events)
    tmp = None
    may_run = False
    try:
        tmp = tempfile.mkdtemp()
        path, wrapper = _wrapper(tmp)
        state = status(session, proc=proc)
        if state == RUNNING:
            raise TuiError(f"session {session} is running; {attach_command(session)}")
        if state is not None:
            kill(session, proc=proc)
        _handover(path, argv, cwd, env)
        cols, rows = shutil.get_terminal_size()
        # a session may run from here on, even when the tmux call fails (new-session ran, set-option didn't)
        may_run = True
        res = _tmux(["new-session", "-d", "-s", session, "-x", str(cols), "-y", str(rows), *wrapper,
                     ";", "set-option", "-p", "-t", f"={session}:", "remain-on-exit", "on"], proc)
        if res.returncode:
            if _err(res).startswith("duplicate session:"):   # another start took the name: not ours to kill
                may_run = False
            raise TuiError(f"tmux: {_err(res)}")
        if not _taken(path, sleep):
            raise TuiError("the session did not start")
        decorate(session, events, status_line=status_line, proc=proc)
        show(session, template, split=split, split_from=split_from, opener=opener, per_column=per_column, proc=proc)
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
    """Run argv in the session's pane in place of its command (respawn-pane -k), handed over as start hands over its
    command, clearing the pane's history in the same tmux command, so no line the old command prints lands in between.
    Returns once it runs."""
    _name(session)
    argv = _command(argv, env)
    tmp = None
    try:
        tmp = tempfile.mkdtemp()
        path, wrapper = _wrapper(tmp)
        _handover(path, argv, cwd, env)
        target = f"={session}:"
        _tmux_ok(["clear-history", "-t", target, ";", "respawn-pane", "-k", "-t", target, *wrapper], proc)
        if not _taken(path, sleep):
            raise TuiError("the pane did not respawn")
    except OSError as e:
        raise TuiError(f"handover: {e}") from e
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _command(argv: list[str], env: dict[str, str]) -> list[str]:
    """argv with argv[0] resolved on env's PATH, made absolute."""
    if not argv:
        raise TuiError("no command")
    exe = shutil.which(argv[0], path=env.get("PATH", os.defpath))
    if exe is None:
        raise TuiError(f"command not found: {argv[0]}")
    return [os.path.abspath(exe), *argv[1:]]


def _wrapper(tmp: str) -> tuple[str, list[str]]:
    """The handover file's path in tmp, and the pane command running EXEC on it."""
    path = os.path.join(tmp, "handover.json")
    for arg in (sys.executable, path):
        if "#" in arg or arg.endswith(";"):
            raise TuiError(f"tmux would misread {arg!r}")
    return path, [sys.executable, "-I", "-c", EXEC, path]


def _handover(path: str, argv: list[str], cwd: str, env: dict[str, str]) -> None:
    """Writes the handover file at path."""
    cwd = os.path.abspath(cwd)
    child = {k: v for k, v in env.items() if k not in (*TERMINAL_KEYS, *PARENT_KEYS)}
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump({"argv": argv, "cwd": cwd, "env": {**child, "PWD": cwd}}, f)


def _taken(path: str, sleep) -> bool:
    """Whether the wrapper takes (unlinks) the handover file within HANDOVER_TIMEOUT."""
    for _ in range(round(HANDOVER_TIMEOUT / POLL)):
        if not os.path.exists(path):
            return True
        sleep(POLL)
    return not os.path.exists(path)


def hooks(events: str | None = None) -> str:
    """The --settings JSON: Stop sets the session's @state to done, PermissionRequest and a Notification but idle_prompt
    to blocked, UserPromptSubmit to working; with events, all but UserPromptSubmit also append
    `HH:MM:SS <session> done|blocked` to it. Each is a no-op outside tmux."""
    def entry(word: str, log: bool = True) -> list:
        cmd = f'tmux set-option -t "$TMUX_PANE" @state {word} >/dev/null 2>&1'
        if log and events is not None:
            cmd = (f'{{ {cmd}; echo "$(date +%H:%M:%S) $(tmux display -p -t "$TMUX_PANE" \'#S\') {word}" '
                   f'>> {shlex.quote(events)}; }}')
        return [{"type": "command", "command": f'[ -n "$TMUX_PANE" ] && {cmd} || true'}]

    return json.dumps({"hooks": {
        "Stop": [{"hooks": entry("done")}],
        "PermissionRequest": [{"hooks": entry("blocked")}],
        "Notification": [{"matcher": MATCHER, "hooks": entry("blocked")}],
        "UserPromptSubmit": [{"hooks": entry("working", log=False)}]}})


def with_hooks(argv: list[str], events: str | None = None) -> list[str]:
    """argv with hooks(events) appended to the hook lists of its first --settings JSON before a bare --, else with
    --settings added after argv[0]."""
    if not argv:
        raise TuiError("no command")
    add = json.loads(hooks(events))["hooks"]
    for i, arg in enumerate(argv[1:], 1):
        if arg == "--":
            break
        if arg != "--settings" and not arg.startswith("--settings="):
            continue
        at, prefix = (i, "--settings=") if arg != "--settings" else (i + 1, "")
        value = argv[at][len(prefix):] if at < len(argv) else ""
        try:
            settings = json.loads(value)
        except ValueError:
            settings = None
        own = settings.setdefault("hooks", {}) if isinstance(settings, dict) else None
        if not isinstance(own, dict) or not all(isinstance(own.get(e, []), list) for e in add):
            raise TuiError(f"--settings {value!r}: want a JSON object with a list of hooks per event")
        for e, entries in add.items():
            own[e] = [*own.get(e, []), *entries]
        return [*argv[:at], prefix + json.dumps(settings), *argv[at + 1:]]
    return [argv[0], "--settings", hooks(events), *argv[1:]]


def events_file(path: str) -> str:
    """The absolute path; creates the file 0600 when missing. Not a regular file of the caller's, or a path tmux would
    misread in set-option: TuiError."""
    path = os.path.abspath(path)
    if "#" in path or path.endswith(";"):
        raise TuiError(f"events file {path}: tmux would misread it")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
    except OSError as e:
        raise TuiError(f"events file {path}: {e.strerror}") from e
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise TuiError(f"events file {path}: not a regular file owned by you")
    return path


def decorate(session: str, events: str | None = None, *, status_line: bool = False, proc=subprocess.run) -> None:
    """On the session: @events (when given), the pane-died hook (@state dead; with events, `HH:MM:SS <session> dead`
    appended to @events), the pane border showing `<session> <state>` and tmux's status line on or off."""
    target = f"={_name(session)}:"
    died = DIED if events is None else f"{DIED} ; {DEAD_EVENT}"
    for args in [*([["set-option", "-t", target, "@events", events]] if events is not None else []),
                 ["set-hook", "-p", "-t", target, "pane-died", died],
                 ["set-option", "-w", "-t", target, "pane-border-status", "top"],
                 ["set-option", "-w", "-t", target, "pane-border-format", BORDER],
                 ["set-option", "-t", target, "status", "on" if status_line else "off"]]:
        _tmux_ok(args, proc)


def status(session: str, *, proc=subprocess.run):
    """None (no such session), RUNNING, or the dead pane's exit status (signal n: 128+n)."""
    res = _tmux(["display-message", "-p", "-t", f"={_name(session)}:",
                 "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"], proc)
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
    _tmux_ok(["kill-session", "-t", f"={_name(session)}"], proc)


def send(session: str, text: str, *, proc=subprocess.run, sleep=time.sleep) -> None:
    """Paste text into the session's pane as a bracketed paste, then press Enter."""
    target = f"={_name(session)}:"
    buffer = f"tui-{session}-{uuid.uuid4().hex}"
    # load-buffer from stdin keeps the text exact: send-keys -l lost the head of long multi-line text, and set-buffer
    # takes a trailing ';' for a command separator
    _tmux_ok(["load-buffer", "-b", buffer, "-"], proc, text)
    _tmux_ok(["paste-buffer", "-p", "-d", "-b", buffer, "-t", target], proc)
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


def attach_command(session: str) -> str:
    """The printed command attaching a terminal to the session: `tmux attach -t '=<session>'`, after
    $TUI_ATTACH_PREFIX (e.g. `docker exec -it <container>`) when that is not blank."""
    prefix = os.environ.get("TUI_ATTACH_PREFIX", "").strip()
    return f"{prefix} tmux attach -t '={session}'" if prefix else f"tmux attach -t '={session}'"


def show(session: str, template: str | None = None, *, split: str | None = None, split_from: str | None = None,
         opener: str | None = None, per_column: int | None = None, proc=subprocess.run) -> str | None:
    """Print how to attach, then run the show: template, else open_pane; "" runs nothing.
    A failure is printed and returned, never raised; only open_pane raises TuiError, for a bad split, split_from,
    opener or per_column."""
    print(f"tui: session {session}: {attach_command(session)}", file=sys.stderr)
    if template is None:
        why = open_pane(session, split=split, split_from=split_from, opener=opener, per_column=per_column, proc=proc)
    else:
        why = _run(session, template, proc) if template else None
    if why:
        print(f"tui: show: {why}", file=sys.stderr)
    return why


def open_pane(session: str, *, split: str | None = None, split_from: str | None = None, opener: str | None = None,
              per_column: int | None = None, proc=subprocess.run) -> str | None:
    """Open a pane attached to the session; the session then records @opener (when known) and @pane. The opener
    defaults to opener(). Grid mode (_grid, a tmux opener's): split the pane _grid picks, set the grid window's options
    and hooks, then tile it in columns of per_column (default PER_COLUMN); split and split_from are ignored, with a
    notice. Else automatic (split and split_from None): below the newest pane still open among the @pane of the
    opener's other sessions (@opener), else right of the opener's pane; or beside `split_from`'s pane (anchor()),
    default the opener's, on side split (default right): an iTerm2 pane splits in iTerm2, else its tmux pane with tmux.
    None or the failure; with no pane to split, the failure names the attach command."""
    _layout(split, split_from, opener)
    _per_column(per_column)
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
    return _grid_up(window, manager, PER_COLUMN if per_column is None else per_column, proc)


def _sessions(proc) -> list[list[str]]:
    """Each session's SESSIONS fields, its id valid; none when list-sessions fails."""
    res = _tmux(["list-sessions", "-F", SESSIONS], proc)
    lines = res.stdout.split("\n") if res.returncode == 0 else []
    return [f for f in (line.split("\t") for line in lines) if len(f) == 5 and SESSION_ID.fullmatch(f[0])]


def _grid(opener: str, own: bool | None, sessions: list[list[str]], proc) -> tuple[str, str, Anchor, str] | None:
    """Grid mode for a pane tmux session opener opens: (grid window, its manager, the pane to split, side); None
    (iTerm2 mode) when not in a grid or tmux fails. The grid window: the opener's @pane's when that is one; else, for a
    root (no @opener) a client shows, its anchor's ($TMUX_PANE when own, else the most recently active client's pane),
    a new grid managed by the anchor when it is none. The pane to split: the largest but the manager, below; else the
    manager, right."""
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
    """pane's window: its id, its manager (@grid-manager when a pane of it, else `manager`), each pane's area, the
    server's socket; None when tmux fails or prints junk."""
    res = _tmux(["list-panes", "-t", pane, "-F", GRID_PANES], proc)
    rows = [GRID_ROW.fullmatch(line) for line in res.stdout.splitlines()] if res.returncode == 0 else []
    if not rows or not all(rows):
        return None
    areas = {m[3]: int(m[4]) * int(m[5]) for m in rows}
    window, stored = rows[0].group(1, 2)
    return window, stored if stored in areas else manager, areas, rows[0][6]


def _grid_up(window: str, manager: str, per_column: int, proc) -> str | None:
    """Set window's grid options and hooks (none, with a notice, when tmux would misread a path in them), then tile
    it. None or the failure."""
    args = ["set-option", "-w", "-t", window, GRID_MANAGER, manager, ";",
            "set-option", "-w", "-t", window, GRID_PER_COLUMN, str(per_column)]
    paths = (sys.executable or "", os.path.abspath(__file__))
    bad = [p for p in paths if not HOOK_PATH.fullmatch(p)]
    if bad:
        print(f"tui: show: grid: hooks: {bad[0]}: tmux would misread it", file=sys.stderr)
    else:
        hook = f"run-shell -b '{paths[0]} -I {paths[1]} tile {window} >/dev/null 2>&1 || true'"
        args += [a for name in GRID_HOOKS for a in (";", "set-hook", "-w", "-t", window, name, hook)]
    try:
        _tmux_ok(args, proc)
        tile(window, per_column=per_column, proc=proc)
    except TuiError as e:
        return str(e)
    return None


def _stack(session: str, opener: str, sessions: list[list[str]], tmux: str,
           proc) -> tuple[str | None, str | None] | None:
    """_split below the newest still open @pane of the opener's other sessions; None when none is open."""
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
    """Split a's pane on side: in iTerm2 when an iTerm2 pane shows it, else with tmux split-window. (the new pane, None)
    or (None, tmux's failure); raises TuiError when there is no pane to split."""
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
    """The new iTerm2 session's unique id; TuiError with the failure."""
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
    """@opener (when known) and @pane on the session, for the next automatic placement; a failure is printed."""
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


def tile(window: str, *, per_column: int | None = None, proc=subprocess.run) -> None:
    """Lay out grid window `window` (@N): the manager left at its current width, _grid_tree's cells in columns of
    per_column (default: the window's @grid-per-column, else PER_COLUMN). Nothing changes when the window is zoomed,
    no grid window, already so laid out or too small, or its layout is unreadable or not its panes'; with its manager
    gone or alone, the grid is torn down (hooks, options)."""
    if not WINDOW.fullmatch(window):
        raise TuiError(f"invalid window id {window!r}: want {WINDOW.pattern}")
    _per_column(per_column)
    fields = _tmux_ok(["display-message", "-p", "-t", window, GRID_WINDOW], proc).stdout.rstrip("\n").split("\t")
    if len(fields) != 6 or fields[2] != "0" or not fields[4]:
        return
    width, height, _, layout, manager, n = fields
    panes = _tmux_ok(["list-panes", "-t", window, "-F", "#{pane_id}"], proc).stdout.split()
    current = _parse_layout(layout)
    leaves = [] if current is None else _leaves(current)
    if current is None or sorted(leaf.pane for leaf in leaves) != sorted(panes):
        return
    if manager not in panes or len(panes) == 1:
        args = ["-u", "-w", "-t", window]
        _tmux_ok([*(a for hook in GRID_HOOKS for a in ("set-hook", *args, hook, ";")),
                  "set-option", *args, GRID_MANAGER, ";", "set-option", *args, GRID_PER_COLUMN], proc)
        return
    sessions = []
    for f in (line.split("\t") for line in _tmux_ok(["list-sessions", "-F", SESSIONS], proc).stdout.split("\n")):
        if len(f) == 5 and SESSION_ID.fullmatch(f[0]) and NAME.fullmatch(f[1]):
            sessions.append((int(f[0][1:]), f[1], f[2] if OPENER.fullmatch(f[2]) else "",
                             f[3] if PANE.fullmatch(f[3]) else ""))
    if per_column is None:
        per_column = int(n) if re.fullmatch(r"[1-9][0-9]{0,3}", n) else PER_COLUMN
    want = _grid_layout(_grid_tree(manager, panes, sessions, current), width=int(width), height=int(height),
                        manager=manager, manager_width=next(c.w for c in leaves if c.pane == manager),
                        per_column=per_column)
    if want is None or _leaves(want) == leaves:
        return
    swaps = [a for source, target in _swaps(panes, [c.pane for c in _leaves(want)])
             for a in ("swap-pane", "-d", "-s", source, "-t", target, ";")]
    _tmux_ok([*swaps, "select-layout", "-t", window, _render_layout(want)], proc)


def _checksum(body: str) -> str:
    csum = 0
    for byte in body.encode():
        csum = ((csum >> 1) + ((csum & 1) << 15) + byte) & 0xFFFF
    return f"{csum:04x}"


def _parse_layout(text: str) -> Cell | None:
    """The cell tree of a layout string, `<checksum>,<body>` (a checksum that must match) or a body; None when
    malformed."""
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
    """`<checksum>,<body>`, as select-layout takes it."""
    body = _body(cell)
    return f"{_checksum(body)},{body}"


def _body(c: Cell) -> str:
    head = f"{c.w}x{c.h},{c.x},{c.y}"
    if c.kind == LEAF:
        return f"{head},{c.pane[1:]}"
    return head + c.kind[0] + ",".join(_body(child) for child in c.children) + c.kind[1]


def _leaves(cell: Cell) -> list[Cell]:
    """Depth first: the order select-layout gives them to the panes, by pane index."""
    return [cell] if cell.kind == LEAF else [leaf for child in cell.children for leaf in _leaves(child)]


def _paths(cell: Cell, above: tuple[Cell, ...] = ()) -> dict[str, list[Cell]]:
    """Each leaf's pane: the cells from the root down to the leaf, depth first."""
    if cell.kind == LEAF:
        return {cell.pane: [*above, cell]}
    return {pane: path for child in cell.children for pane, path in _paths(child, (*above, cell)).items()}


def _grid_tree(manager: str, panes: list[str], sessions: list[tuple[int, str, str, str]],
               layout: Cell) -> list[Slot]:
    """The main grid's cells, in order. A node (a session whose @pane is a window pane but the manager; two claiming
    one: the lower session id) sits in its @opener's node; nodes whose @opener is gone form an orphan group, in the
    cell the current layout shows them in; the rest, and what main does not reach (a group whole), sit in main.
    Siblings by pane id, an orphan group where the layout has it; manual panes last. sessions: each live session's
    (id, name, @opener, @pane), fullmatched, "" when unset; layout's leaves: panes."""
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
            # R holds the columns, unless one main pane is left: then R is that pane's own cell
            if head.pane == manager or cell is columns and mains != [head.pane]:
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
    """The grid window's layout: the manager left, full height, manager_width wide (half when that is the window's)
    but narrowed till the columns fit; cells (at least one) in columns of per_column, top down, then right. None when
    it does not fit."""
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
    """A pane with children: it takes the left half, they stack in the right; an orphan group's members stack."""
    if not s.children:
        return Cell(w, h, x, y, s.pane)
    if s.pane is None:
        return _line(s.children, TOP_BOTTOM, w, h, x, y)
    left = w // 2
    return Cell(w, h, x, y, kind=LEFT_RIGHT, children=(
        Cell(left, h, x, y, s.pane), _line(s.children, TOP_BOTTOM, w - left - 1, h, x + left + 1, y)))


def _line(slots: tuple[Slot, ...], kind: str, w: int, h: int, x: int, y: int) -> Cell:
    """slots side by side (LEFT_RIGHT) or stacked, equal sizes, the remainder to the first; one: its own cell."""
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
    """The fewest (source, target) swap-pane pairs turning pane index order have into want: each puts want's next
    pane in place."""
    have, swaps = list(have), []
    for i, pane in enumerate(want):
        if have[i] != pane:
            j = have.index(pane)
            swaps.append((pane, have[i]))
            have[i], have[j] = pane, have[i]
    return swaps


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tui_claude.py", description="Host claude in a detached tmux session: start it, "
                                 "type into it, read it, show it. The show: --show T; else, opened from tmux, a grid "
                                 "in the manager's window (it stays left, the panes it opens fill columns of "
                                 "--per-column, top down, then right); else an iTerm2 split, else a tmux split. T may "
                                 "use {{session}}; '' prints only the attach command. "
                                 "$TUI_ATTACH_PREFIX (e.g. 'docker exec -it C') prefixes printed attach commands.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    start_p = sub.add_parser("start", help="run claude in a new detached session, then show it",
                             description="session -- claude [args...]: everything after the first -- is the "
                                         "command, passed through verbatim but for the hooks start merges into its "
                                         "--settings, so it must be claude.")
    _show_options(start_p)
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
            start(a.session, command, cwd=os.getcwd(), env=dict(os.environ), events=a.events, template=a.show,
                  split=a.split, split_from=a.split_from, per_column=a.per_column)
        elif a.cmd == "send":
            send(a.session, a.text)
        elif a.cmd == "read":
            print(read(a.session, a.lines))
        elif a.cmd == "tile":
            tile(a.window)
        else:
            if status(a.session) is None:
                raise TuiError(f"no session {a.session}")
            return 1 if show(a.session, a.show, split=a.split, split_from=a.split_from, per_column=a.per_column) else 0
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
    """The tmux session of the caller's pane ($TMUX_PANE); None outside tmux."""
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
    """The caller's opener: its tmux session (own_session()), else, outside tmux with $TERM_PROGRAM iTerm.app, the
    full $ITERM_SESSION_ID. Raises TuiError when there is neither or it does not match OPENER."""
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
    """The pane a show goes beside: the pane a terminal shows tmux session `split_from` in (_shown), else the caller's
    session's (_opener_pane), else $ITERM_SESSION_ID's iTerm2 pane (outside tmux only with $TERM_PROGRAM iTerm.app: a
    leftover variable names another pane). Raises TuiError when there is none."""
    if split_from is not None:
        found = _shown(split_from, False, proc)
        if found is None:
            raise TuiError(f"no terminal shows tmux session {split_from}")
        return found
    own = own_session(proc=proc)
    return _iterm_pane("not in tmux") if own is None else _opener_pane(own, True, proc)


def _opener_pane(opener: str, own: bool | None, proc) -> Anchor:
    """An iTerm2 opener's pane by its unique id; a tmux opener's: _shown (own: it is the caller's session; None: ask
    tmux), else $ITERM_SESSION_ID's."""
    if ":" in opener:
        return Anchor(("id", [opener.partition(":")[2]]))
    if own is None:
        own = own_session(proc=proc) == opener
    return _shown(opener, own, proc) or _iterm_pane(f"no terminal shows tmux session {opener}")


def _shown(session: str, own: bool, proc) -> Anchor | None:
    """The pane a terminal shows the session in, by its most recently active client: a nested client's live host pane,
    else the caller's own pane ($TMUX_PANE) when own, else the client's pane. A control-mode client (iTerm2's tmux -CC)
    gets only a tmux pane, which iTerm2 draws as a native split: its tty is the hidden gateway tab's. None when no
    client shows it."""
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
    """The clients showing the session, most recently active first: CLIENTS' fields."""
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


if __name__ == "__main__":
    sys.exit(main())
