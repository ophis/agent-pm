"""Hosts Claude Code TUIs: runs claude in a detached tmux session another agent or a person can watch and drive.

One file, tmux 3.3+ plus the Python stdlib (3.9+): copy it anywhere. CLI: python3 tui_claude.py --help.
Session names are [A-Za-z0-9_-]+. Errors raise TuiError.
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
# whose panes stack together: a tmux session, or an iTerm2 pane by its full $ITERM_SESSION_ID (w0t0p0:<unique id>)
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
MATCHER = "permission_prompt|elicitation_dialog|agent_needs_input"
DIED = "set-option @state dead"
# tmux expands run-shell's #{...} when the hook fires; q: shell-quotes the name and @events, so neither runs as code.
DEAD_EVENT = "run-shell -b 'echo \"$(date +%H:%M:%S)\" #{q:session_name} dead >> #{q:@events}'"
BORDER = " #{session_name} #{@state} "
CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{socket_path}"
PANES = "#{pane_dead} #{pane_tty} #{pane_id}"
SESSIONS = "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}"
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
# Set in a Claude Code session's commands: a claude inheriting it saves no transcript and can't be resumed.
CHILD_SESSION = "CLAUDE_CODE_CHILD_SESSION"
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
    """The pane a show goes beside. iterm: the iTerm2 panes that may show it, ("tty", client ttys, most recently active
    first) or ("id", [unique id]); None when it is a tmux pane showing a nested client. pane, socket: the tmux pane split
    when no iTerm2 pane shows it, and its server (None for an "id" anchor). session: the tmux session it was found by."""
    iterm: tuple[str, list[str]] | None
    pane: str | None = None
    socket: str | None = None
    session: str | None = None


def start(session: str, argv: list[str], *, cwd: str, env: dict[str, str], events: str | None = None,
          template: str | None = None, split: str | None = None, split_from: str | None = None, opener: str | None = None,
          status_line: bool = False, proc=subprocess.run, sleep=time.sleep) -> None:
    """Run with_hooks(argv, events), argv a claude command, in a new detached session, in cwd with env minus
    CHILD_SESSION, its PWD set to cwd, plus the pane's terminal keys; once it runs, decorate the session (status_line
    too), then show it. events goes through events_file first. argv, cwd and env reach the pane through a 0600 handover
    file, never through tmux. Raising, it leaves no session of its own."""
    _name(session)
    if template is None:
        _layout(split, split_from, opener)
    if not argv:
        raise TuiError("no command")
    exe = shutil.which(argv[0], path=env.get("PATH", os.defpath))
    if exe is None:
        raise TuiError(f"command not found: {argv[0]}")
    if events is not None:
        events = events_file(events)
    argv = with_hooks(argv, events)
    tmp = None
    may_run = False
    try:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "handover.json")
        for arg in (sys.executable, path):
            if "#" in arg or arg.endswith(";"):
                raise TuiError(f"tmux would misread {arg!r}")
        state = status(session, proc=proc)
        if state == RUNNING:
            raise TuiError(f"session {session} is running; {attach_command(session)}")
        if state is not None:
            kill(session, proc=proc)
        cwd = os.path.abspath(cwd)
        child = {k: v for k, v in env.items() if k not in (*TERMINAL_KEYS, CHILD_SESSION)}
        handover = {"argv": [os.path.abspath(exe), *argv[1:]], "cwd": cwd, "env": {**child, "PWD": cwd}}
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump(handover, f)
        cols, rows = shutil.get_terminal_size()
        # a session may run from here on, even when the tmux call fails (new-session ran, set-option didn't)
        may_run = True
        res = _tmux(["new-session", "-d", "-s", session, "-x", str(cols), "-y", str(rows),
                     sys.executable, "-I", "-c", EXEC, path,
                     ";", "set-option", "-p", "-t", f"={session}:", "remain-on-exit", "on"], proc)
        if res.returncode:
            if _err(res).startswith("duplicate session:"):   # another start took the name: not ours to kill
                may_run = False
            raise TuiError(f"tmux: {_err(res)}")
        for _ in range(round(HANDOVER_TIMEOUT / POLL)):
            if not os.path.exists(path):
                break
            sleep(POLL)
        if os.path.exists(path):
            raise TuiError("the session did not start")
        decorate(session, events, status_line=status_line, proc=proc)
        show(session, template, split=split, split_from=split_from, opener=opener, proc=proc)
        may_run = False
    except OSError as e:
        raise TuiError(f"handover: {e}") from e
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)   # first, so a late wrapper finds no file to run
        if may_run:
            with contextlib.suppress(TuiError):
                kill(session, proc=proc)


def hooks(events: str | None = None) -> str:
    """The --settings JSON: Stop, a blocking Notification and UserPromptSubmit set the session's @state to done, blocked
    and working; with events, Stop and Notification also append `HH:MM:SS <session> done|blocked` to it. Each is a
    no-op outside tmux."""
    def entry(word: str, log: bool = True) -> list:
        cmd = f'tmux set-option -t "$TMUX_PANE" @state {word} >/dev/null 2>&1'
        if log and events is not None:
            cmd = (f'{{ {cmd}; echo "$(date +%H:%M:%S) $(tmux display -p -t "$TMUX_PANE" \'#S\') {word}" '
                   f'>> {shlex.quote(events)}; }}')
        return [{"type": "command", "command": f'[ -n "$TMUX_PANE" ] && {cmd} || true'}]

    return json.dumps({"hooks": {
        "Stop": [{"hooks": entry("done")}],
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
    """End the session."""
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
         opener: str | None = None, proc=subprocess.run) -> str | None:
    """Print how to attach, then run the show: template, else open_pane; "" runs nothing.
    A failure is printed and returned, never raised; only open_pane raises TuiError, for a bad split, split_from or
    opener."""
    print(f"tui: session {session}: {attach_command(session)}", file=sys.stderr)
    if template is None:
        why = open_pane(session, split=split, split_from=split_from, opener=opener, proc=proc)
    else:
        why = _run(session, template, proc) if template else None
    if why:
        print(f"tui: show: {why}", file=sys.stderr)
    return why


def open_pane(session: str, *, split: str | None = None, split_from: str | None = None, opener: str | None = None,
              proc=subprocess.run) -> str | None:
    """Open a pane attached to the session. Automatic (split and split_from None): below the newest pane still open among
    the @pane of the opener's other sessions (@opener), else right of the opener's pane. Else beside `split_from`'s pane
    (anchor()), default the opener's, on side split (default right). The opener defaults to opener(). An iTerm2 pane
    splits in iTerm2, else its tmux pane with tmux; then the session records @opener (when known) and @pane.
    None or the failure; with no pane to split, the failure names the attach command."""
    _layout(split, split_from, opener)
    tmux = shutil.which("tmux")
    if tmux is None:
        return "tmux not found"
    tmux = os.path.abspath(tmux)
    own = None
    try:
        if opener is None:
            try:
                opener, own = _opener(proc), True
            except TuiError:
                if split_from is None:
                    raise
        if split_from is not None:
            new, why = _split(session, split or "right", anchor(split_from, proc=proc), tmux, proc)
        elif split is not None:
            new, why = _split(session, split, _opener_pane(opener, own, proc), tmux, proc)
        else:
            new, why = (_stack(session, opener, tmux, proc)
                        or _split(session, "right", _opener_pane(opener, own, proc), tmux, proc))
    except TuiError as e:
        return f"{e}; watch it with {attach_command(session)}"
    if new is None:
        return why
    _record(session, opener, new, proc)
    return None


def _stack(session: str, opener: str, tmux: str, proc) -> tuple[str | None, str | None] | None:
    """_split below the newest still open @pane of the opener's other sessions; None when none is open."""
    res = _tmux(["list-sessions", "-F", SESSIONS], proc)
    rows = []
    for f in (line.split("\t") for line in (res.stdout.split("\n") if res.returncode == 0 else [])):
        if (len(f) == 5 and SESSION_ID.fullmatch(f[0]) and f[1] != session and f[2] == opener
                and (PANE.fullmatch(f[3]) or ITERM_ID.fullmatch(f[3]))):
            rows.append((int(f[0][1:]), f[3], f[4]))
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tui_claude.py", description="Host claude in a detached tmux session: start it, "
                                 "type into it, read it, show it. The show: --show T, else an iTerm2 split, else a tmux "
                                 "split; T may use {{session}}; '' prints only the attach command. "
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
                  split=a.split, split_from=a.split_from)
        elif a.cmd == "send":
            send(a.session, a.text)
        elif a.cmd == "read":
            print(read(a.session, a.lines))
        else:
            if status(a.session) is None:
                raise TuiError(f"no session {a.session}")
            return 1 if show(a.session, a.show, split=a.split, split_from=a.split_from) else 0
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
    else the caller's own pane ($TMUX_PANE) when own, else the client's pane. None when no client shows it."""
    out = _tmux_ok(["list-clients", "-t", f"={session}", "-F", CLIENTS], proc).stdout
    clients = sorted((c for c in (line.split(" ", 3) for line in out.splitlines())
                      if len(c) == 4 and c[0].isdigit() and c[1] and PANE.fullmatch(c[2]) and c[3]),
                     key=lambda c: int(c[0]), reverse=True)
    if not clients:
        return None
    _, tty, pane, socket = clients[0]
    hosts = {f[1]: f[2] for f in (line.split(" ", 2) for line in
                                  _tmux_ok(["list-panes", "-a", "-F", PANES], proc).stdout.splitlines())
             if len(f) == 3 and f[0] != "1"}
    if PANE.fullmatch(hosts.get(tty, "")):
        return Anchor(None, hosts[tty], socket, session)
    return Anchor(("tty", [c[1] for c in clients]), os.environ["TMUX_PANE"] if own else pane, socket, session)


def _iterm_pane(why: str) -> Anchor:
    unique = os.environ.get("ITERM_SESSION_ID", "").partition(":")[2]
    if not unique or not (os.environ.get("TMUX") or os.environ.get("TERM_PROGRAM") == "iTerm.app"):
        raise TuiError(NO_PANE.format(why))
    return Anchor(("id", [unique]))


def _show_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--show", metavar="T", help="the show command, instead of the split")
    p.add_argument("--split", choices=SPLITS, help="the split's side (default: below the newest pane you opened that "
                                                    "still shows, else right of yours; with --split-from, right)")
    p.add_argument("--split-from", metavar="S", type=_session_arg,
                   help="split the pane showing tmux session S (default: yours)")


def _session_arg(value: str) -> str:
    try:
        return _name(value)
    except TuiError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


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
