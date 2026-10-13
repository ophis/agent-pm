"""Tmux command expectations and fake process boundaries."""

import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import unittest
import unittest.mock
from contextlib import contextmanager

from agent_pm.session.hosts.tmux import layout as tmux_layout
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.model import types as tmux_model

RUNTIME = re.compile(r"claude|codex|print_prompt", re.I)


FORMAT = "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"


CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{client_control_mode} #{socket_path}"


PGREP = ["pgrep", "-a", "-x", "iTerm2"]


ITERM = {"ITERM_SESSION_ID": "w0t0p0:ABC", "TERM_PROGRAM": "iTerm.app"}


TMUX_KW = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}


SH_KW = {"stdin": subprocess.DEVNULL, "timeout": 30}


OSA_KW = {**TMUX_KW, "timeout": 30}


TMUX = '/opt/a"b\\c/bin/tmux'  # the script text must never carry it


ATTACH = "tui: session s: tmux attach -t '=s'\n"


WATCH = "; watch it with tmux attach -t '=s'"


ABSENT = (
    "can't find session: =s",
    "no server running on /tmp/tmux-501/default",
    "error connecting to /tmp/tmux-501/default (No such file or directory)",
)


FAILED = ("error connecting to /tmp/tmux-501/default (Permission denied)", "boom", "")


SIGNALS = [int(signal.SIGHUP), int(signal.SIGTERM), int(signal.SIGINT)]


DIED = "set-option @state dead"


DIED_EVENTS = (
    DIED + " ; run-shell -b 'echo \"$(date +%H:%M:%S)\" #{q:session_name} dead >> #{q:@events}'"
)


INSIDE = {"TMUX": "/tmp/tmux-501/default,1,0", "TMUX_PANE": "%3", "ITERM_SESSION_ID": "w0t0p0:ABC"}


OWN = ["tmux", "display-message", "-p", "-t", "%3", "#{session_name}"]


SESSIONS = [
    "tmux",
    "list-sessions",
    "-F",
    "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}",
]


LIVE = ["tmux", "list-panes", "-a", "-F", "#{pane_id}"]


HOSTS = ["tmux", "list-panes", "-a", "-F", "#{pane_dead} #{pane_tty} #{pane_id}"]


SOCK = "/tmp/s p"


PREFIXES = (
    ({}, ""),
    ({"TUI_ATTACH_PREFIX": " \t"}, ""),
    ({"TUI_ATTACH_PREFIX": " docker exec -it box "}, "docker exec -it box "),
)


MAIN_VERTICAL = "7f31,160x48,0,0{80x48,0,0,0,79x48,81,0[79x24,81,0,1,79x23,81,25,2]}"


TILED_SPLIT = (
    "9a18,160x48,0,0[160x15,0,0{79x15,0,0,0,40x15,80,0,1,39x15,121,0,5},"
    "160x15,0,16{79x15,0,16,2,80x15,80,16,3},160x16,0,32,4]"
)


GRID_PANES = (
    "#{window_id}\t#{@grid-manager}\t#{pane_id}\t#{pane_width}\t#{pane_height}\t#{socket_path}"
)


PY, SCRIPT = (
    "/usr/bin/python3",
    "/opt/a_b.c@1+2-3/tmux_host.py",
)  # sys.executable and the module's path, patched


GRID_HOOKS = ("pane-exited", "window-resized", "window-layout-changed")


MODE_HOOKS = {
    "all": GRID_HOOKS,
    "open-close": ("pane-exited", "window-layout-changed"),
    "off": (),
}  # each mode's


RETILE_ERROR = "^retile must be one of all, open-close, off, not {}$"


def done(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


def record(opener, pane):
    """The set-options recording session s's opener (when known) and new pane."""
    return [
        *([["tmux", "set-option", "-t", "=s:", "@opener", opener]] if opener else []),
        ["tmux", "set-option", "-t", "=s:", "@pane", pane],
    ]


def split_window(flag, pane, sock=SOCK):
    return [
        "tmux",
        "split-window",
        "-d",
        flag,
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        pane,
        "env",
        "-u",
        "TMUX",
        TMUX,
        "-S",
        sock,
        "attach",
        "-t",
        "=s",
    ]


def clients_of(session):
    return ["tmux", "list-clients", "-t", f"={session}", "-F", CLIENTS]


def grid_read(pane):
    return ["tmux", "list-panes", "-t", pane, "-F", GRID_PANES]


def grid_set(window, manager, n=3, hooks=True, retile="all"):
    """The one tmux call setting a grid window's options, unsetting @grid-busy, then per grid hook
    in order: with hooks, setting each of retile's MODE_HOOKS (each starts a no-wait tile unless
    @grid-busy is the window, under open-close only when the pane set changed); unsetting each other
    one.
    """
    flags = "--no-wait --if-panes-changed" if retile == "open-close" else "--no-wait"
    hook = (
        f"if-shell -F '#{{==:#{{@grid-busy}},{window}}}' '' "
        f"\"run-shell -b '{PY} -I {SCRIPT} tile {flags} {window} >/dev/null 2>&1 || true'\""
    )
    mode = MODE_HOOKS[retile]
    return [
        "tmux",
        "set-option",
        "-w",
        "-t",
        window,
        "@grid-manager",
        manager,
        ";",
        "set-option",
        "-w",
        "-t",
        window,
        "@grid-per-column",
        str(n),
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        window,
        "@grid-busy",
        *(
            a
            for h in GRID_HOOKS
            if hooks or h not in mode
            for a in (
                (";", "set-hook", "-w", "-t", window, h, hook)
                if h in mode
                else (";", "set-hook", "-u", "-w", "-t", window, h)
            )
        ),
    ]


def mute(window):
    return ["tmux", "set-option", "-w", "-t", window, "@grid-busy", window]


def unmute(window):
    return ["tmux", "set-option", "-u", "-w", "-t", window, "@grid-busy"]


def environ(**values):
    return unittest.mock.patch.dict(os.environ, values, clear=True)


def clock(test, on_sleep=lambda n: None):
    """time.monotonic and time.sleep patched for test, the clock at 1000 s: a sleep advances it,
    then calls on_sleep(sleeps so far). Returns the slept seconds.
    """
    now, slept = [1000.0], []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds
        on_sleep(len(slept))

    test.enterContext(unittest.mock.patch("time.monotonic", side_effect=lambda: now[0]))
    test.enterContext(unittest.mock.patch("time.sleep", side_effect=sleep))
    return slept


def hold(test, path):
    """Another holder of the flock on path till test ends (path created when missing). Returns its
    release.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    test.addCleanup(os.close, fd)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return lambda: fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def deadline(seconds=5):
    """Fails a block still running after seconds (a blocked open is interrupted) instead of hanging."""

    def expire(*_):
        raise AssertionError(
            f"still running after {seconds} s"
        )  # not an OSError, which _grid_lock makes a TuiError

    old = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def which(tmux=TMUX):
    real = shutil.which
    return unittest.mock.patch(
        "shutil.which",
        side_effect=lambda cmd, *a, **kw: tmux if cmd == "tmux" else real(cmd, *a, **kw),
    )


def osa(split, kind, session, *anchors):
    return ["osascript", "-e", tmux_model.APPLESCRIPT, split, kind, TMUX, session, *anchors]


def decorations(session, events=None, status="off"):
    target = f"={session}:"
    return [
        *([["tmux", "set-option", "-t", target, "@events", events]] if events else []),
        ["tmux", "set-hook", "-p", "-t", target, "pane-died", DIED_EVENTS if events else DIED],
        ["tmux", "set-option", "-w", "-t", target, "pane-border-status", "top"],
        [
            "tmux",
            "set-option",
            "-w",
            "-t",
            target,
            "pane-border-format",
            " #{session_name} #{@state} ",
        ],
        ["tmux", "set-option", "-t", target, "status", status],
    ]


def chained(calls):
    return [arg for call in calls for arg in (";", *call[1:])]


def panes(*rows):
    """A list-panes result printing rows of (pane_dead, pane_tty, pane_id) in the call's -F format."""

    def result(argv):
        fmt = argv[argv.index("-F") + 1]
        values = [
            dict(zip(("pane_dead", "pane_tty", "pane_id"), row, strict=False)) for row in rows
        ]
        return 0, "".join(
            re.sub(r"#\{(\w+)\}", lambda m, *, v=v: v[m.group(1)], fmt) + "\n" for v in values
        )

    return result


class Tmux:
    """Plays tmux, and sh and osascript: records each call. `results` maps a tmux command (after any
    -u) or another program to its (rc, stdout, stderr), an exception to raise, or a function of the
    argv giving either. `pane` is what display-message prints (None: no server). On new-session it
    reads the handover file named in its argv (after EXEC); unless `hand_over` is False it plays the
    wrapper on the errpipe named next: opens it, unlinks the handover, then per `wrapper` closes it
    ("exec"), writes that errno (bytes: as given) and closes it, or keeps it open ("hang", fd
    `writer`).
    """

    def __init__(self, pane=None, fail=(), hand_over=True, results=None, wrapper="exec"):
        self.pane, self.fail, self.hand_over, self.results = pane, fail, hand_over, results or {}
        self.wrapper = wrapper
        self.calls, self.kwargs = [], []
        self.path = self.handover = self.mode = self.dir_mode = self.dir_at_kill = None
        self.fifo = self.fifo_mode = self.writer = None

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        key = argv[0] if argv[0] != "tmux" else argv[2] if argv[1] == "-u" else argv[1]
        if key in self.results:
            result = self.results[key]
            if callable(result):
                result = result(argv)
            if isinstance(result, BaseException):
                raise result
            return done(argv, *result)
        if argv[1] == "kill-session" and self.path:
            self.dir_at_kill = os.path.isdir(os.path.dirname(self.path))
        if argv[1] in self.fail:
            return done(argv, 1, err="boom\n")
        if argv[1] == "display-message":
            return (
                done(argv, 1, err="no server running\n")
                if self.pane is None
                else done(argv, 0, self.pane + "\n")
            )
        if argv[1] == "new-session":
            at = argv.index(tmux_host.EXEC)
            self.path, self.fifo = argv[at + 1 : at + 3]
            self.mode = stat.S_IMODE(os.stat(self.path).st_mode)
            self.dir_mode = stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode)
            self.fifo_mode = os.stat(self.fifo).st_mode
            with open(self.path) as f:
                self.handover = json.load(f)
            if self.hand_over:
                self.writer = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.unlink(self.path)
                if self.wrapper != "hang":
                    if self.wrapper != "exec":
                        data = self.wrapper
                        os.write(self.writer, data if isinstance(data, bytes) else b"%d" % data)
                    os.close(self.writer)
        return done(argv)

    def commands(self):
        return [c[1] if c[0] == "tmux" else c[0] for c in self.calls]


MARK_KW = {**TMUX_KW, "timeout": 5}


LISTING = [
    "tmux",
    "-u",
    "list-sessions",
    "-F",
    "#{session_name}\t#{pane_dead}\t#{@sid}\t#{@runtime}\t#{@state}\t#{@pane}\t#{@opener}",
]


COLUMNS = ("name", "dead", "sid", "runtime", "state", "pane", "opener")


ALL_CLIENTS = [
    "tmux",
    "-u",
    "list-clients",
    "-F",
    "#{client_activity}\t#{client_tty}\t#{session_name}",
]


HOST_PANES = ["tmux", "-u", "list-panes", "-a", "-F", "#{pane_dead}\t#{pane_tty}\t#{session_name}"]


def tabbed(*rows, width=3):
    """A tmux result printing each row's fields tab-separated, padded with empty ones to width."""
    return 0, "".join("\t".join((*row, *[""] * (width - len(row)))) + "\n" for row in rows)


def listed(*rows):
    return tabbed(*rows, width=len(COLUMNS))


def captured(name):
    return ["tmux", "capture-pane", "-p", "-J", "-t", f"={name}:", "-S", "-2000"]


def row(name, **fields):
    return {
        "name": name,
        "sid": None,
        "runtime": None,
        "state": None,
        "pane": None,
        "opener": None,
        "dead": False,
        "shown": None,
        "last_lines": None,
        **fields,
    }


def seq(*results):
    """A Tmux result giving each of results in turn."""
    it = iter(results)
    return lambda argv: next(it)


def slot(pane, *children):
    return tmux_host.Slot(pane, children)


def body(cell):
    return tmux_layout._render_layout(cell).split(",", 1)[1]


def dump(text):
    """A layout body as #{window_layout} prints it, with its checksum."""
    return f"{tmux_layout._checksum(text)},{text}"


def grid(cells, per_column=3, width=160, height=48, manager_width=79):
    """The grid of manager %0 in a width x height window; cells: main cells, or n for %1..%n."""
    if isinstance(cells, int):
        cells = [slot(f"%{i}") for i in range(1, cells + 1)]
    return tmux_layout._grid_layout(
        cells,
        "%0",
        geometry=tmux_host.GridGeometry(
            width=width, height=height, manager_width=manager_width, per_column=per_column
        ),
    )


WAIT = """import os, sys, time
for _ in range(3000):
    if os.path.exists(sys.argv[1]): break
    time.sleep(0.01)"""
