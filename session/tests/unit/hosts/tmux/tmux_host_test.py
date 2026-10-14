"""hosts/tmux/tmux_host.py: every tmux, sh, pgrep and osascript call through a fake proc (no tmux runs here):
start (no pane), detach, status, listing, display_error, tmux_path, paste/keys/send, read, option, pane, process, mark,
the events file, the show and its placement, the grid's tile, the CLI, the pane wrapper (EXEC)'s text and the file's imports
(running them: its integration test)."""

import ast
import contextlib
import errno
import fcntl
import io
import itertools
import json
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout, suppress
from types import SimpleNamespace

from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.tmux_host import TuiError

from session.tests.case import Case
from utils.tests.process_fixtures import DEVNULL, TimeoutExpired

RUNTIME = re.compile(r"claude|codex|print_prompt", re.I)

FORMAT = "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"

CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{client_control_mode} #{socket_path}"

PGREP = ["pgrep", "-a", "-x", "iTerm2"]

ITERM = {"ITERM_SESSION_ID": "w0t0p0:ABC", "TERM_PROGRAM": "iTerm.app"}

TMUX_KW = {"capture_output": True, "text": True, "stdin": DEVNULL}

SH_KW = {"stdin": DEVNULL, "timeout": 30}

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

NEW_PROCESS = "4242 $3 %7 4343"

NEW = ["-P", "-F", "#{pid} #{session_id} #{pane_id} #{pane_pid}"]

EXITED = (
    'run-shell \'echo "$(date +%H:%M:%S)" #{q:session_name} exited'
    " #{?pane_dead_signal,-#{?#{m:*[!0-9]*,#{pane_dead_signal}},$(kill -l #{q:pane_dead_signal}),"
    "#{pane_dead_signal}},#{pane_dead_status}} >> #{q:@events}' ; kill-session"
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
    return SimpleNamespace(args=argv, returncode=rc, stdout=out, stderr=err)


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


def grid_set(window, manager, n=3, *, hooks=True, retile="all"):
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


def clock(test, on_sleep=lambda _n: None):
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
    """Fail after the deadline, interrupting a blocked open."""

    def expire(*_):
        raise AssertionError(
            f"still running after {seconds} s"
        )  # An OSError would become a TuiError.

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
    return ["osascript", "-e", tmux_host.APPLESCRIPT, split, kind, TMUX, session, *anchors]


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


def expected_panes(*rows):
    """Print (pane_dead, pane_tty, pane_id) rows in the list-panes call's -F format."""

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

    def __init__(self, pane=None, fail=(), *, hand_over=True, results=None, wrapper="exec"):
        self.pane, self.fail, self.hand_over, self.results = pane, fail, hand_over, results or {}
        self.wrapper = wrapper
        self.calls, self.kwargs = [], []
        self.path = self._handover = self.mode = self.dir_mode = self.dir_at_kill = None
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
            self.take_launch(argv)
            if "-P" in argv:
                return done(argv, 0, NEW_PROCESS + "\n")
        return done(argv)

    def take_launch(self, argv):
        at = argv.index(tmux_host.EXEC)
        self.path, self.fifo = argv[at + 1 : at + 3]
        self.mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.dir_mode = stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode)
        self.fifo_mode = os.stat(self.fifo).st_mode
        with open(self.path) as f:
            self._handover = json.load(f)
        if self.hand_over:
            self.writer = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.unlink(self.path)
            if self.wrapper != "hang":
                if self.wrapper != "exec":
                    data = self.wrapper
                    os.write(self.writer, data if isinstance(data, bytes) else b"%d" % data)
                os.close(self.writer)

    def commands(self):
        return [c[1] if c[0] == "tmux" else c[0] for c in self.calls]


MARK_KW = {**TMUX_KW, "timeout": 5}

LISTING = [
    "tmux",
    "-u",
    "list-sessions",
    "-F",
    "#{session_name}\t#{pane_dead}\t#{@sid}\t#{@runtime}\t#{@state}\t#{@pane}\t#{@opener}\t#{@display}",
]

COLUMNS = ("name", "dead", "sid", "runtime", "state", "pane", "opener", "display")

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


def expected_row(name, **fields):
    return {
        "name": name,
        "sid": None,
        "runtime": None,
        "state": None,
        "pane": None,
        "opener": None,
        "display": None,
        "dead": False,
        "shown": None,
        "last_lines": None,
        **fields,
    }


def seq(*results):
    """A Tmux result giving each of results in turn."""
    it = iter(results)
    return lambda _argv: next(it)


def slot(pane, *children):
    return tmux_host.Slot(pane, children)


def body(cell):
    return tmux_host._render_layout(cell).split(",", 1)[1]


def dump(text):
    """A layout body as #{window_layout} prints it, with its checksum."""
    return f"{tmux_host._checksum(text)},{text}"


def grid(cells, per_column=3, width=160, height=48, manager_width=79):
    """The grid of manager %0 in a width x height window; cells: main cells, or n for %1..%n."""
    if isinstance(cells, int):
        cells = [slot(f"%{i}") for i in range(1, cells + 1)]
    return tmux_host._grid_layout(
        cells, "%0", width=width, height=height, manager_width=manager_width, per_column=per_column
    )


def opening_commands(*, window="@1", manager="%3", opener="mgr", n=3, retile="all", hooks=True):
    return [
        *record(opener, "%20"),
        grid_set(window, manager, n, hooks=hooks, retile=retile),
        *([] if retile == "off" else [["tile", window, n]]),
    ]


class StartFixture(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.bin, self.temp, self.cwd = (os.path.join(self.root, d) for d in ("bin", "temp", "cwd"))
        for d in (self.bin, self.temp, self.cwd):
            os.makedirs(d)
        self.tool = os.path.join(self.bin, "tool")
        with open(self.tool, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.tool, 0o755)
        for p in (
            unittest.mock.patch.object(tempfile, "tempdir", self.temp),
            unittest.mock.patch(
                "shutil.get_terminal_size", return_value=os.terminal_size((120, 40))
            ),
            unittest.mock.patch.dict(os.environ),
        ):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("TUI_ATTACH_PREFIX", None)
        self.sleep = unittest.mock.Mock()
        self.stderr = io.StringIO()

    def start(self, fake, session="s", argv=("tool", "-x", "a b"), env=None, **kw):
        env = {"PATH": self.bin, "HOME": "/h"} if env is None else env
        with redirect_stderr(self.stderr):
            tmux_host.start(
                session, list(argv), **kw, proc=fake, sleep=self.sleep, cwd=self.cwd, env=env
            )


class PaneFixture(Case):
    def show(self, fake, env, **kw):
        self.stderr = io.StringIO()
        with environ(**env), which(kw.pop("tmux", TMUX)), redirect_stderr(self.stderr):
            return tmux_host.show(
                "s", kw.pop("template", None), **kw, proc=fake, script=tmux_host.__file__
            )

    def clients(self, *rows):
        """A list-clients result: rows of (activity, tty, pane[, control mode, default "0"])."""
        rows = [(*r, "0") if len(r) == 3 else r for r in rows]
        return 0, "".join(
            f"{at} {tty} {pane} {control} {SOCK}\n" for at, tty, pane, control in rows
        )


class TileFixture(Case):
    """Window @3, 160x48: manager %0 (session mgr) left, 79 wide; w<i>'s @pane is %<i>. The server's
    socket is in a temp dir, @3's lock file beside it; the clock (clock()) at 1000 s, i.e. 1000000
    ms.
    """

    SOCKET = ["tmux", "display-message", "-p", "-t", "@3", "#{socket_path}"]

    READ = [
        "tmux",
        "display-message",
        "-p",
        "-t",
        "@3",
        (
            "#{window_width}\t#{window_height}\t#{window_zoomed_flag}\t#{window_layout}\t#{@grid-manager}\t"
            "#{@grid-per-column}\t#{@grid-panes}\t#{@grid-tiles}"
        ),
    ]

    PANES = ["tmux", "list-panes", "-t", "@3", "-F", "#{pane_id}"]

    TEARDOWN = [
        "tmux",
        "set-hook",
        "-u",
        "-w",
        "-t",
        "@3",
        "pane-exited",
        ";",
        "set-hook",
        "-u",
        "-w",
        "-t",
        "@3",
        "window-resized",
        ";",
        "set-hook",
        "-u",
        "-w",
        "-t",
        "@3",
        "window-layout-changed",
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        "@3",
        "@grid-manager",
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        "@3",
        "@grid-per-column",
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        "@3",
        "@grid-tiles",
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        "@3",
        "@grid-busy",
        ";",
        "set-option",
        "-u",
        "-w",
        "-t",
        "@3",
        "@grid-panes",
    ]

    UNMUTE = ["tmux", "set-option", "-u", "-w", "-t", "@3", "@grid-busy"]

    THREE = "0a6c,160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17,2,80x15,80,33,3]}"

    W3 = [("$2", "w1", "mgr", "%1"), ("$3", "w2", "mgr", "%2"), ("$4", "w3", "mgr", "%3")]

    WIDE = (
        "8b77,160x48,0,0{100x48,0,0,0,59x48,101,0[59x24,101,0,1,59x23,101,25,2]}",
        ["%0", "%1", "%2"],
        [("$2", "w1", "mgr", "%1"), ("$3", "a", "w1", "%2")],
    )

    WIDE_SENT = [
        "tmux",
        "select-layout",
        "-t",
        "@3",
        "e37e,160x48,0,0{100x48,0,0,0,59x48,101,0{29x48,101,0,1,29x48,131,0,2}}",
    ]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sock = os.path.join(tmp.name, "default")
        self.lock = self.sock + "-3.lock"
        clock(self)

    def fake(
        self,
        layout,
        panes,
        rows=(),
        *,
        manager="%0",
        n="",
        zoomed="0",
        size=(160, 48),
        tiles="",
        sock=None,
        recorded=None,
        fail=(),
        message=(0,),
        **results,
    ):
        """A tmux printing these for @3, its socket `sock` (default self.sock), `message` the result
        of another display-message; rows: (session id, name, @opener, @pane) besides mgr's;
        recorded: @grid-panes (None: panes' pane set, kept as self.set).
        """
        rows = [("$1", "mgr", "", ""), *rows]
        self.set = " ".join(sorted(panes, key=lambda p: int(p[1:])))
        recorded = self.set if recorded is None else recorded
        printed = {
            self.SOCKET[-1]: (0, f"{self.sock if sock is None else sock}\n"),
            self.READ[-1]: (
                0,
                "\t".join((*map(str, size), zoomed, layout, manager, n, recorded, tiles)) + "\n",
            ),
        }
        return Tmux(
            fail=fail,
            results={
                "display-message": lambda argv: printed.get(argv[-1], message),
                "list-panes": (0, "".join(f"{p}\n" for p in panes), ""),
                "list-sessions": (0, "".join("\t".join((*row, SOCK)) + "\n" for row in rows), ""),
                **results,
            },
        )

    def tile(
        self, layout, panes, rows=(), *, per_column=None, wait=True, if_panes_changed=False, **kw
    ):
        """tile @3 given what tmux prints (fake's arguments). Returns the fake."""
        fake = self.fake(layout, panes, rows, **kw)
        tmux_host.tile(
            "@3", per_column=per_column, wait=wait, if_panes_changed=if_panes_changed, proc=fake
        )
        return fake

    def applied(self, send, times="1000000"):
        """The two tmux calls of a re-tile sending `send` (a tmux call of swap-panes, then
        select-layout): @grid-tiles set to times, @grid-panes to the last fake's pane set and
        @grid-busy to @3 before it in one command, then @grid-busy unset.
        """
        return [
            [
                "tmux",
                "set-option",
                "-w",
                "-t",
                "@3",
                "@grid-tiles",
                times,
                ";",
                "set-option",
                "-w",
                "-t",
                "@3",
                "@grid-panes",
                self.set,
                ";",
                "set-option",
                "-w",
                "-t",
                "@3",
                "@grid-busy",
                "@3",
                ";",
                *send[1:],
            ],
            self.UNMUTE,
        ]


class PaneAnchorTest(PaneFixture):
    def test_anchor_by_iterm_session_id(self):
        for split in ("right", "below"):
            with self.subTest(split=split):
                fake = Tmux(results={"osascript": (0, "ok NEW\n")})
                self.assertIsNone(self.show(fake, ITERM, split=split))
                self.assertEqual(
                    fake.calls, [PGREP, osa(split, "id", "s", "ABC"), *record("w0t0p0:ABC", "NEW")]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW, OSA_KW, TMUX_KW, TMUX_KW])
                self.assertEqual(self.stderr.getvalue(), ATTACH)
        fake, err = Tmux(results={"osascript": (0, "ok NEW")}), io.StringIO()
        with environ(**ITERM), which(), redirect_stderr(err):
            self.assertIsNone(
                tmux_host.open_pane("s", split="below", proc=fake, script=tmux_host.__file__)
            )
        self.assertEqual(
            (fake.calls[:2], err.getvalue()), ([PGREP, osa("below", "id", "s", "ABC")], "")
        )

    def test_iterm2_pane_showing_own_session(self):
        fake = Tmux(
            results={
                "display-message": (0, "own\n"),
                "osascript": (0, "ok NEW\n"),
                "list-clients": self.clients(
                    (100, "/dev/ttys001", "%1"),
                    (300, "/dev/ttys003", "%2"),
                    (200, "/dev/ttys002", "%9"),
                ),
            }
        )
        self.assertIsNone(self.show(fake, INSIDE))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("own"),
                HOSTS,
                PGREP,
                osa("right", "tty", "s", "/dev/ttys003", "/dev/ttys002", "/dev/ttys001"),
                *record("own", "NEW"),
            ],
        )

    def test_tmux_split_of_own_pane_when_no_iterm2_pane_shows_it(self):
        clients = self.clients((5, "/dev/ttys004", "%9"))
        for split, flag in (("right", "-h"), ("below", "-v")):
            for not_iterm in (
                {"pgrep": (1,)},
                {"pgrep": FileNotFoundError(2, "No such file", "pgrep")},
                {"osascript": (0, "no iTerm2 pane shows the anchor\n")},
                {"osascript": (1, "", "syntax error\n")},
            ):
                with self.subTest(split=split, not_iterm=not_iterm):
                    fake = Tmux(
                        results={
                            "display-message": (0, "own\n"),
                            "list-clients": clients,
                            "split-window": (0, "%10\n"),
                            **not_iterm,
                        }
                    )
                    self.assertIsNone(self.show(fake, INSIDE, split=split))
                    self.assertEqual(
                        fake.calls[-3:], [split_window(flag, "%3"), *record("own", "%10")]
                    )
                    self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_detached_own_session_falls_back_to_iterm_session_id(self):
        fake = Tmux(
            results={
                "display-message": (0, "own\n"),
                "list-clients": (0, ""),
                "osascript": (0, "ok NEW\n"),
            }
        )
        self.assertIsNone(self.show(fake, INSIDE))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("own"),
                PGREP,
                osa("right", "id", "s", "ABC"),
                *record("own", "NEW"),
            ],
        )

    def test_anchor(self):
        own = {"display-message": (0, "own\n"), "list-clients": (0, "")}
        shown = self.clients((5, "/dev/ttys004", "%9"))
        control = self.clients((5, "/dev/ttys004", "%9", "1"))
        mixed = self.clients((5, "/dev/ttys004", "%9"), (3, "/dev/ttys006", "%8", "1"))
        nested = expected_panes(("0", "/dev/ttys001", "%1"), ("0", "/dev/ttys004", "%5"))
        cases = [
            (ITERM, {}, None, tmux_host.ItermAnchor(("id", ["ABC"]))),
            (
                INSIDE,
                {**own, "list-clients": shown},
                None,
                tmux_host.ItermAnchor(
                    ("tty", ["/dev/ttys004"]), tmux_host.TmuxAnchor("%3", SOCK, "own")
                ),
            ),
            (
                INSIDE,
                {**own, "list-clients": shown, "list-panes": nested},
                None,
                tmux_host.TmuxAnchor("%5", SOCK, "own"),
            ),
            (INSIDE, own, None, tmux_host.ItermAnchor(("id", ["ABC"]))),
            (
                {},
                {"list-clients": shown},
                "b",
                tmux_host.ItermAnchor(
                    ("tty", ["/dev/ttys004"]), tmux_host.TmuxAnchor("%9", SOCK, "b")
                ),
            ),
            (
                {},
                {"list-clients": shown, "list-panes": nested},
                "b",
                tmux_host.TmuxAnchor("%5", SOCK, "b"),
            ),
            (
                {},
                {
                    "list-clients": (
                        0,
                        "5 /dev/ttys004\n7 /dev/ttys005 junk 0 /s\n7 /dev/ttys005 %7 /s\n"
                        "7 /dev/ttys005 %7 2 /s\n" + shown[1],
                    )
                },
                "b",
                tmux_host.ItermAnchor(
                    ("tty", ["/dev/ttys004"]), tmux_host.TmuxAnchor("%9", SOCK, "b")
                ),
            ),
            (
                INSIDE,
                {**own, "list-clients": control, "list-panes": nested},
                None,
                tmux_host.TmuxAnchor("%3", SOCK, "own"),
            ),
            (
                {},
                {"list-clients": control, "list-panes": nested},
                "b",
                tmux_host.TmuxAnchor("%9", SOCK, "b"),
            ),
            (
                {},
                {"list-clients": mixed},
                "b",
                tmux_host.ItermAnchor(
                    ("tty", ["/dev/ttys004"]), tmux_host.TmuxAnchor("%9", SOCK, "b")
                ),
            ),
        ]
        for env, results, split_from, want in cases:
            with self.subTest(env=env, results=results, split_from=split_from), environ(**env):
                self.assertEqual(tmux_host.anchor(split_from, proc=Tmux(results=results)), want)
        no_iterm = {k: v for k, v in INSIDE.items() if k != "ITERM_SESSION_ID"}
        cases = [
            ({}, {}, None, "no anchor pane: not in tmux"),
            ({"ITERM_SESSION_ID": "w0t0p0"}, {}, None, "no anchor pane"),
            ({"ITERM_SESSION_ID": "w0t0p0:ABC"}, {}, None, "no anchor pane"),
            (
                {"ITERM_SESSION_ID": "w0t0p0:ABC", "TERM_PROGRAM": "vscode"},
                {},
                None,
                "no anchor pane",
            ),
            (no_iterm, own, None, "no anchor pane: no terminal shows tmux session own"),
            (INSIDE, {"list-clients": (0, "")}, "b", "^no terminal shows tmux session b$"),
        ]
        for env, results, split_from, msg in cases:
            with (
                self.subTest(env=env, split_from=split_from),
                environ(**env),
                self.assertRaisesRegex(tmux_host.TuiError, msg),
            ):
                tmux_host.anchor(split_from, proc=Tmux(results=results))

    def test_a_dead_pane_sharing_the_clients_tty_is_skipped(self):
        # a dead worker pane keeps its tty, which a later terminal may get
        shown = self.clients((5, "/dev/ttys004", "%9"))
        live, dead = ("0", "/dev/ttys004", "%5"), ("1", "/dev/ttys004", "%6")
        for rows, want in (
            ([live, dead], tmux_host.TmuxAnchor("%5", SOCK, "b")),
            (
                [dead],
                tmux_host.ItermAnchor(
                    ("tty", ["/dev/ttys004"]), tmux_host.TmuxAnchor("%9", SOCK, "b")
                ),
            ),
        ):
            with self.subTest(rows=rows), environ():
                fake = Tmux(results={"list-clients": shown, "list-panes": expected_panes(*rows)})
                self.assertEqual(tmux_host.anchor("b", proc=fake), want)

    def test_own_session(self):
        fake = Tmux(results={"display-message": (0, "own\n")})
        with environ(**INSIDE):
            self.assertEqual(tmux_host.own_session(proc=fake), "own")
        self.assertEqual(fake.calls, [OWN])
        fake = Tmux()
        with environ():
            self.assertIsNone(tmux_host.own_session(proc=fake))
        self.assertEqual(fake.calls, [])
        with (
            environ(**{**INSIDE, "TMUX_PANE": "%3;"}),
            self.assertRaisesRegex(tmux_host.TuiError, "TMUX_PANE"),
        ):
            tmux_host.own_session(proc=fake)

    def test_bad_tmux_pane(self):
        for env in (
            {**INSIDE, "TMUX_PANE": pane} for pane in ("", "3", "%3;", "%x", "%3 ", "#{x}")
        ):
            with self.subTest(pane=env["TMUX_PANE"]):
                fake = Tmux()
                self.assertRegex(self.show(fake, env), r"TMUX_PANE")
                self.assertEqual(fake.calls, [])
        fake = Tmux()
        self.assertRegex(self.show(fake, {"TMUX": "x"}), r"TMUX_PANE")
        self.assertEqual(fake.calls, [])

    def test_own_session_name_checked(self):
        fake = Tmux(results={"display-message": (0, "a;\n")})
        self.assertRegex(self.show(fake, INSIDE), r"'a;'")
        self.assertEqual(fake.commands(), ["display-message"])


class OpenerTest(Case):
    def test_in_tmux_its_session(self):
        fake = Tmux(results={"display-message": (0, "cmd\n")})
        with environ(**INSIDE, TERM_PROGRAM="iTerm.app"):
            self.assertEqual(tmux_host.opener(proc=fake), "cmd")
        self.assertEqual(fake.calls, [OWN])

    def test_in_iterm2_the_full_iterm_session_id(self):
        for value in ("w0t0p0:ABC", "w0t1p0:6D7E8F90-1A2B-4C3D-9E8F-0A1B2C3D4E5F"):
            fake = Tmux()
            with (
                self.subTest(value=value),
                environ(ITERM_SESSION_ID=value, TERM_PROGRAM="iTerm.app"),
            ):
                self.assertEqual(tmux_host.opener(proc=fake), value)
            self.assertEqual(fake.calls, [])

    def test_neither(self):
        for env in (
            {},
            {"ITERM_SESSION_ID": "w0t0p0:ABC"},
            {**ITERM, "TERM_PROGRAM": "vscode"},
            {"TERM_PROGRAM": "iTerm.app"},
            {"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": ""},
        ):
            fake = Tmux()
            with (
                self.subTest(env=env),
                environ(**env),
                self.assertRaisesRegex(
                    tmux_host.TuiError,
                    r"^no anchor pane: not in tmux, no iTerm2 pane \(\$ITERM_SESSION_ID\)$",
                ),
            ):
                tmux_host.opener(proc=fake)
            self.assertEqual(fake.calls, [])

    def test_a_bad_iterm_session_id(self):
        for value in (
            "w0t0p0",
            "w0t0p0:",
            ":ABC",
            "w0t0p0:a;b",
            "w0:t0:x",
            "a b:c",
            "w0t0p0:#{x}",
            "w0_t0:x",
        ):
            with (
                self.subTest(value=value),
                environ(ITERM_SESSION_ID=value, TERM_PROGRAM="iTerm.app"),
                self.assertRaisesRegex(tmux_host.TuiError, r"^bad \$ITERM_SESSION_ID"),
            ):
                tmux_host.opener(proc=Tmux())

    def test_bad_tmux_pane(self):
        with (
            environ(**{**INSIDE, "TMUX_PANE": "%3;"}),
            self.assertRaisesRegex(tmux_host.TuiError, "TMUX_PANE"),
        ):
            tmux_host.opener(proc=Tmux())

    def test_patterns(self):
        for value in ("cmd", "a_b-1", "w0t0p0:ABC", "w0t1p0:6D7E-8F"):
            self.assertTrue(tmux_host.OPENER.fullmatch(value), value)
        for value in ("", "a b", "a:", ":b", "a:b:c", "a_b:c", "a:b_c", "a;", "%1", "a\n"):
            self.assertFalse(tmux_host.OPENER.fullmatch(value), value)
        self.assertTrue(tmux_host.ITERM_ID.fullmatch("6D7E-8F"))
        for value in ("", "a_b", "a:b", "%1", "a b"):
            self.assertFalse(tmux_host.ITERM_ID.fullmatch(value), value)


class CliTest(Case):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = tmux_host.main(list(argv), operations=getattr(self, "actions", {}))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def patch(self, name, **kw):
        if not hasattr(self, "actions"):
            self.actions = {}
        self.actions[name] = unittest.mock.Mock(**kw)
        return self.actions[name]

    def test_start_forms(self):
        # the command verbatim; start shows nothing, so it takes no show option
        start = self.patch("start")
        for argv, session, command, events in (
            ("start a -- cmd", "a", ["cmd"], None),
            ("start a -- cmd -- x", "a", ["cmd", "--", "x"], None),
            ("start a --events e -- cmd --settings {}", "a", ["cmd", "--settings", "{}"], "e"),
            ("start --events e b -- cmd --x -- -p", "b", ["cmd", "--x", "--", "-p"], "e"),
        ):
            with self.subTest(argv=argv):
                start.reset_mock()
                self.assertEqual(self.main(*argv.split())[0], 0)
                start.assert_called_once_with(
                    session, command, events=events, cwd=os.getcwd(), env=dict(os.environ)
                )

    def test_start_usage_errors(self):
        start = self.patch("start")
        for argv in (
            "start a",
            "start a --",
            "start a --bogus x -- cmd",
            "start a=b -- cmd",
            "start a cmd",
            "start -- cmd",
            "start a b -- cmd",
            "start --events -- a -- cmd",
            "start --show T a -- cmd",
            "start --split below a -- cmd",
            "start --split-from b a -- cmd",
            "start --per-column 2 a -- cmd",
            "start --grid-retile off a -- cmd",
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv.split())[0], 2)
        for argv in ("start a", "start a cmd", "start a --events e cmd"):
            with self.subTest(argv=argv):
                self.assertIn("a command must follow --", self.main(*argv.split())[2])
        start.assert_not_called()

    @unittest.mock.patch.dict(
        os.environ, {"NO_COLOR": "1"}
    )  # beats FORCE_COLOR, which colors argparse's help (3.14)
    def test_help(self):
        rc, out, _ = self.main("-h")
        self.assertEqual(rc, 0)
        self.assertIn("{start,send,read,show,tile}", out)
        self.assertIn("$TUI_ATTACH_PREFIX", out)
        rc, out, _ = self.main("start", "-h")
        self.assertEqual(rc, 0)
        out = " ".join(out.split())
        self.assertIn("session -- command", out)
        self.assertIn("--events FILE", out)
        for option in ("--show", "--split", "--per-column", "--grid-retile"):
            self.assertNotIn(option, out)
        for cmd in ((), ("start",), ("send",), ("read",), ("show",), ("tile",)):
            with self.subTest(cmd=cmd):
                self.assertIsNone(RUNTIME.search(self.main(*cmd, "-h")[1]))

    def test_subcommands(self):
        send, read, show, status = (self.patch(n) for n in ("send", "read", "show", "status"))
        read.return_value, show.return_value, status.return_value = (
            "pane\ntext",
            None,
            tmux_host.PaneStatus.RUNNING,
        )
        self.assertEqual(self.main("send", "s", "hi there"), (0, "", ""))
        send.assert_called_once_with("s", "hi there")
        self.assertEqual(self.main("send", "s", "--", "-x")[0], 0)
        self.assertEqual(send.call_args, unittest.mock.call("s", "-x"))
        self.assertEqual(self.main("read", "s"), (0, "pane\ntext\n", ""))
        self.assertEqual(self.main("read", "s", "--lines", "3")[0], 0)
        self.assertEqual(
            read.call_args_list, [unittest.mock.call("s", None), unittest.mock.call("s", 3)]
        )
        self.assertEqual(
            self.main("show", "--show", "x", "--split", "below", "--split-from", "b", "s"),
            (0, "", ""),
        )
        status.assert_called_once_with("s")
        show.assert_called_once_with(
            "s", "x", split="below", split_from="b", per_column=3, retile="open-close"
        )
        self.main("show", "s")
        self.assertEqual(
            show.call_args,
            unittest.mock.call(
                "s", None, split=None, split_from=None, per_column=3, retile="open-close"
            ),
        )

    @unittest.mock.patch.dict(os.environ, {"NO_COLOR": "1"})
    def test_per_column(self):
        show = self.patch("show", return_value=None)
        self.patch("status", return_value=tmux_host.PaneStatus.RUNNING)
        for value, n in (("1", 1), ("2", 2), ("9999", 9999), ("0003", 3)):
            with self.subTest(value=value):
                self.assertEqual(self.main("show", "--per-column", value, "s")[0], 0)
                self.assertEqual(show.call_args.kwargs["per_column"], n)
        show.reset_mock()
        for value in ("0", "10000", "00000", "-1", "x", "", "2.0", "\u00b2", " 2"):
            with self.subTest(value=value):
                rc, _, err = self.main("show", "--per-column", value, "s")
                self.assertEqual(rc, 2)
                self.assertIn("--per-column", err)
        show.assert_not_called()
        self.assertIn("--per-column N", " ".join(self.main("show", "-h")[1].split()))

    @unittest.mock.patch.dict(os.environ, {"NO_COLOR": "1"})
    def test_grid_retile(self):
        show = self.patch("show", return_value=None)
        self.patch("status", return_value=tmux_host.PaneStatus.RUNNING)
        for mode in ("all", "open-close", "off"):
            with self.subTest(mode=mode):
                self.assertEqual(self.main("show", "--grid-retile", mode, "s")[0], 0)
                self.assertEqual(show.call_args.kwargs["retile"], mode)
        show.reset_mock()
        for value in ("x", "", "ALL", "open_close", " off"):
            with self.subTest(value=value):
                rc, _, err = self.main("show", "--grid-retile", value, "s")
                self.assertEqual(rc, 2)
                self.assertIn(
                    f"--grid-retile: invalid choice: {value!r} (choose from 'all', 'open-close', "
                    "'off')",
                    " ".join(err.split()),
                )
        show.assert_not_called()
        self.assertIn("--grid-retile M", " ".join(self.main("show", "-h")[1].split()))

    @unittest.mock.patch.dict(os.environ, {"NO_COLOR": "1"})
    def test_split_choices(self):
        show = self.patch("show")
        rc, _, err = self.main("show", "--split", "up", "s")
        self.assertEqual(rc, 2)
        self.assertIn(
            "--split: invalid choice: 'up' (choose from 'right', 'below')", " ".join(err.split())
        )
        show.assert_not_called()

    def test_tile(self):
        real, fake = tmux_host.tile, Tmux()
        tile = self.patch("tile", return_value=None)
        self.assertEqual(self.main("tile", "@3"), (0, "", ""))
        self.assertEqual(self.main("tile", "--no-wait", "@3"), (0, "", ""))
        self.assertEqual(self.main("tile", "--no-wait", "--if-panes-changed", "@3"), (0, "", ""))
        self.assertEqual(self.main("tile", "--if-panes-changed", "@3"), (0, "", ""))
        call = unittest.mock.call
        self.assertEqual(
            tile.call_args_list,
            [
                call("@3", wait=True, if_panes_changed=False),
                call("@3", wait=False, if_panes_changed=False),
                call("@3", wait=False, if_panes_changed=True),
                call("@3", wait=True, if_panes_changed=True),
            ],
        )
        self.assertIn("--if-panes-changed", self.main("tile", "-h")[1])
        tile.side_effect = lambda window, wait, if_panes_changed: real(
            window, wait=wait, if_panes_changed=if_panes_changed, proc=fake
        )
        self.assertEqual(
            self.main("tile", "3"), (1, "", "tui: invalid window id '3': want @[0-9]+\n")
        )
        self.assertEqual(fake.calls, [])

    def test_exit_1(self):
        for name in ("start", "send", "read", "tile"):
            self.patch(name, side_effect=tmux_host.TuiError("tmux: boom"))
        for argv in ("start a -- cmd", "send s hi", "read s", "tile @3"):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv.split()), (1, "", "tui: tmux: boom\n"))
        status, show = (
            self.patch("status", return_value=None),
            self.patch("show", return_value="boom"),
        )
        self.assertEqual(self.main("show", "s"), (1, "", "tui: no session s\n"))
        show.assert_not_called()
        status.return_value = tmux_host.PaneStatus.RUNNING
        self.assertEqual(self.main("show", "s")[0], 1)

    def test_exit_2(self):
        called = [self.patch(n) for n in ("start", "send", "read", "show", "status", "tile")]
        for argv in (
            (),
            ("bogus",),
            ("send", "s"),
            ("send", "a b", "x"),
            ("read", "s", "--lines", "0"),
            ("read", "s", "--lines", "x"),
            ("read", "s", "--lines", "-1"),
            ("show", "--split", "up", "s"),
            ("show", "--split-from", "a b", "s"),
            ("tile",),
            ("tile", "--no-wait"),
            ("tile", "@3", "@4"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv)[0], 2)
        for mock in called:
            mock.assert_not_called()


class SingleFileTest(Case):
    def setUp(self):
        with open(tmux_host.__file__) as f:
            self.source = f.read()

    def test_imports_only_the_stdlib(self):
        names = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add("." * node.level + (node.module or "").split(".")[0])
        self.assertLessEqual(names, set(sys.stdlib_module_names))

    def test_no_runtime_name(self):
        self.assertEqual(RUNTIME.findall(self.source), [])


class DetachTest(StartFixture):
    """Session d hosting argv as old drive.detach did: no decoration, no dead pane kept."""

    def detach(self, fake, session="d", argv=("tool", "-x", "a b"), env=None, **kw):
        env = {"PATH": self.bin, "HOME": "/h"} if env is None else env
        return tmux_host.detach(
            session, list(argv), **kw, proc=fake, sleep=self.sleep, cwd=self.cwd, env=env
        )

    def test_new_session_runs_the_wrapper_and_returns_its_process_once_argv_execd(self):
        fake = Tmux()
        self.assertEqual(self.detach(fake), NEW_PROCESS)
        self.assertEqual(
            fake.calls,
            [
                [
                    "tmux",
                    "new-session",
                    "-d",
                    *NEW,
                    "-s",
                    "d",
                    sys.executable,
                    "-I",
                    "-c",
                    tmux_host.EXEC,
                    fake.path,
                    fake.fifo,
                    ";",
                    "set-option",
                    "-t",
                    "=d:",
                    "@kind",
                    "detach",
                ]
            ],
        )
        self.assertEqual(fake.kwargs, [TMUX_KW])
        self.assertNotIn("remain-on-exit", fake.calls[0])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_with_the_blocked_signals(self):
        env = {
            "PATH": self.bin,
            "HOME": "/h",
            "CHILD_SESSION": "1",
            "PWD": "/elsewhere",
            **{k: "x" for k in tmux_host.TERMINAL_KEYS},
        }
        for block, want in (((), []), ((signal.SIGHUP, signal.SIGTERM, signal.SIGINT), SIGNALS)):
            with self.subTest(block=block):
                fake = Tmux()
                self.detach(fake, env=env, block=block)
                self.assertEqual(
                    fake._handover,
                    {
                        "argv": [self.tool, "-x", "a b"],
                        "cwd": self.cwd,
                        "env": {
                            "PATH": self.bin,
                            "HOME": "/h",
                            "CHILD_SESSION": "1",
                            "PWD": self.cwd,
                        },
                        "block": want,
                    },
                )
                self.assertEqual(fake.mode, 0o600)
                self.assertEqual(fake.dir_mode, 0o700)
                self.assertEqual(os.listdir(self.temp), [])

    def test_the_envs_iterm_session_id_goes_to_the_session_only_in_its_iterm2_form(self):
        for value, given in (
            ("w0t0p0:ABC", True),
            ("w0t1p0:6D7E-8F", True),
            (None, False),
            ("", False),
            ("w0t0p0", False),
            ("cmd", False),
            ("w0t0p0:a;b", False),
            ("w0t0p0:ABC\n", False),
            (":ABC", False),
            ("w0t0p0:A_B", False),
        ):
            with self.subTest(value=value):
                fake = Tmux()
                env = {"PATH": self.bin, **({} if value is None else {"ITERM_SESSION_ID": value})}
                self.detach(fake, env=env)
                head = [
                    "tmux",
                    "new-session",
                    "-d",
                    *NEW,
                    *(["-e", f"ITERM_SESSION_ID={value}"] if given else []),
                    "-s",
                    "d",
                ]
                self.assertEqual(fake.calls[0][: len(head) + 1], [*head, sys.executable])
                self.assertNotIn("ITERM_SESSION_ID", fake._handover["env"])

    def test_opener_set_in_the_same_call(self):
        for opener in ("mgr", "w0t0p0:ABC"):
            with self.subTest(opener=opener):
                fake = Tmux()
                self.detach(fake, opener=opener)
                self.assertEqual(
                    fake.calls,
                    [
                        [
                            "tmux",
                            "new-session",
                            "-d",
                            *NEW,
                            "-s",
                            "d",
                            sys.executable,
                            "-I",
                            "-c",
                            tmux_host.EXEC,
                            fake.path,
                            fake.fifo,
                            ";",
                            "set-option",
                            "-t",
                            "=d:",
                            "@kind",
                            "detach",
                            ";",
                            "set-option",
                            "-t",
                            "=d:",
                            "@opener",
                            opener,
                        ]
                    ],
                )

    def test_events_keep_the_pane_for_the_exited_hook_and_tags_set_in_the_same_call(self):
        events, line = os.path.join(self.root, "events"), '{"v":1,"name":"d"}'
        fake = Tmux()
        self.detach(fake, opener="mgr", display="mgr/right", events=events, record=line)
        self.assertEqual(
            fake.calls[0][fake.calls[0].index(fake.fifo) + 1 :],
            [
                ";",
                "set-option",
                "-p",
                "-t",
                "=d:",
                "remain-on-exit",
                "on",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@events",
                events,
                ";",
                "set-hook",
                "-p",
                "-t",
                "=d:",
                "pane-died",
                EXITED,
                ";",
                "set-option",
                "-t",
                "=d:",
                "@kind",
                "detach",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@opener",
                "mgr",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@display",
                "mgr/right",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@record",
                line,
            ],
        )
        self.assertEqual(stat.S_IMODE(os.stat(events).st_mode), 0o600)

    def test_a_display_or_record_is_stored_without_events_and_no_pane_kept(self):
        fake = Tmux()
        self.detach(fake, display="elsewhere:1.2", record="{}")
        self.assertEqual(
            fake.calls[0][fake.calls[0].index(fake.fifo) + 1 :],
            [
                ";",
                "set-option",
                "-t",
                "=d:",
                "@kind",
                "detach",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@display",
                "elsewhere:1.2",
                ";",
                "set-option",
                "-t",
                "=d:",
                "@record",
                "{}",
            ],
        )

    def test_the_exited_hook_is_a_constant_reading_its_values_only_through_tmux_formats(self):
        self.assertEqual(tmux_host.EXITED_EVENT, EXITED)
        self.assertEqual(
            set(re.findall(r"#\{[^{}]*\}", EXITED)),
            {
                "#{q:session_name}",
                "#{pane_dead_signal}",
                "#{q:pane_dead_signal}",
                "#{pane_dead_status}",
                "#{q:@events}",
            },
        )
        self.assertEqual(EXITED.count("{"), EXITED.count("#{"))
        self.assertEqual(EXITED.count("{"), EXITED.count("}"))
        hooks = []
        for session, events in (("d", "e1"), ("other-name", "e 2 'x'")):
            fake = Tmux()
            self.detach(fake, session=session, events=os.path.join(self.root, events))
            hooks.append(fake.calls[0][fake.calls[0].index("pane-died") + 1])
        self.assertEqual(hooks, [EXITED, EXITED])

    def test_a_bad_display_record_or_events_is_refused_before_tmux(self):
        target, link = os.path.join(self.root, "target"), os.path.join(self.root, "link")
        open(target, "w").close()
        os.symlink(target, link)
        for kw, error in (
            ({"display": "mgr\tx"}, "^display 'mgr\\\\tx': want printable text not ending in ;$"),
            ({"display": "mgr\nx"}, "^display "),
            ({"display": "mgr;"}, "^display "),
            ({"display": 5}, "^display 5: "),
            ({"record": "{};"}, "^tag '@record'"),
            ({"record": "a\0b"}, "^tag '@record'"),
            ({"events": link}, f"^events file {re.escape(link)}: "),
            ({"events": os.path.join(self.root, "a#b")}, "tmux would misread"),
        ):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, error):
                    self.detach(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_a_new_session_tmux_does_not_name_is_killed(self):
        for out in ("", "4242 $3 %7\n", "4242 $3 %7 x\n"):
            with self.subTest(out=out):
                fake = Tmux(results={"new-session": (0, out, "")})
                with self.assertRaisesRegex(tmux_host.TuiError, "^tmux: bad new session "):
                    self.detach(fake)
                self.assertEqual(fake.commands(), ["new-session", "kill-session"])
                self.assertEqual(os.listdir(self.temp), [])

    def test_an_existing_session_is_refused_untouched(self):
        fake = Tmux(results={"new-session": (1, "", "duplicate session: d\n")})
        with self.assertRaisesRegex(tmux_host.TuiError, r"^session d exists$"):
            self.detach(fake, opener="mgr")
        self.assertEqual(fake.commands(), ["new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_not_taken_kills_it(self):
        for fail in ((), {"kill-session"}):
            with self.subTest(fail=fail):
                self.sleep.reset_mock()
                fake = Tmux(fail=fail, hand_over=False)
                with self.assertRaisesRegex(tmux_host.TuiError, r"^session d did not start$"):
                    self.detach(fake)
                self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
                self.assertIs(fake.dir_at_kill, expr2=False)
                self.assertAlmostEqual(
                    sum(c.args[0] for c in self.sleep.call_args_list), tmux_host.HANDOVER_TIMEOUT
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_an_exec_failure_raises_its_error_and_kills_the_session(self):
        fake = Tmux(wrapper=errno.ENOENT)
        with self.assertRaisesRegex(
            tmux_host.TuiError,
            "^" + re.escape(f"cannot run {self.tool}: {os.strerror(errno.ENOENT)}") + "$",
        ):
            self.detach(fake)
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_never_execd_kills_it(self):
        fake = Tmux(wrapper="hang")
        with self.assertRaisesRegex(tmux_host.TuiError, r"^session d did not start$"):
            self.detach(fake)
        os.close(fake.writer)
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.assertAlmostEqual(
            sum(c.args[0] for c in self.sleep.call_args_list), tmux_host.HANDOVER_TIMEOUT
        )
        self.assertEqual(os.listdir(self.temp), [])

    def test_a_failed_tmux_call_kills_the_session_it_may_have_made(self):
        fake = Tmux(
            fail={"new-session"}
        )  # e.g. the chained set-option failed after new-session made the session
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            self.detach(fake, opener="mgr")
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.assertEqual(os.listdir(self.temp), [])

    def test_an_interrupt_after_new_session_kills_the_session(self):
        def interrupt(*_args, **_kw):
            raise KeyboardInterrupt

        fake, self.sleep = Tmux(hand_over=False), interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.detach(fake)
        self.assertEqual(fake.commands(), ["new-session", "kill-session"])
        self.assertIs(fake.dir_at_kill, expr2=False)
        self.assertEqual(os.listdir(self.temp), [])

    def test_refused_before_tmux(self):
        for kw, error in (
            ({"argv": ()}, "^no command$"),
            ({"argv": ("no-such-tool",)}, "no-such-tool"),
            ({"argv": ("tool", "a\0b")}, "NUL"),
            ({"argv": ("tool", "\ud800")}, "encoded"),
            ({"opener": "a b"}, "opener"),
            ({"opener": "a:b:c"}, "opener"),
            ({"opener": ""}, "opener"),
        ):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, error):
                    self.detach(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])
        with (
            unittest.mock.patch.object(sys, "executable", "/opt/py#3/bin/python3"),
            self.assertRaisesRegex(tmux_host.TuiError, "tmux would misread"),
        ):
            self.detach(fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_unwritable(self):
        fake = Tmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", os.path.join(self.root, "missing")),
            self.assertRaisesRegex(tmux_host.TuiError, "^handover: "),
        ):
            self.detach(fake)
        self.assertEqual(fake.calls, [])

    def test_cwd_never_reaches_tmux(self):
        self.cwd = os.path.join(self.root, "#(x)#{y}")
        os.makedirs(self.cwd)
        fake = Tmux()
        self.detach(fake)
        self.assertEqual(fake._handover["cwd"], self.cwd)
        self.assertEqual([a for c in fake.calls for a in c if "#(x)" in a or "#{y}" in a], [])


class ExecTest(Case):
    def test_safe_for_tmux(self):
        self.assertNotIn("#", tmux_host.EXEC)
        self.assertFalse(tmux_host.EXEC.rstrip().endswith(";"))


class MarkTest(Case):
    """The event protocol a hook writes: the pane's @state, and done or blocked lines in the events
    file.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.events = os.path.join(self.root, "events")
        with open(self.events, "w") as f:
            f.write("old\n")
        self.enterContext(environ(TMUX_PANE="%1"))

    def text(self):
        with open(self.events) as f:
            return f.read()

    def test_working_sets_the_state_only(self):
        fake = Tmux(pane="w1")
        tmux_host.mark("working", self.events, proc=fake)
        self.assertEqual(fake.calls, [["tmux", "set-option", "-t", "%1", "@state", "working"]])
        self.assertEqual(fake.kwargs, [MARK_KW])
        self.assertEqual(self.text(), "old\n")

    def test_done_and_blocked_set_the_state_and_append_a_line_through_the_given_tmux(self):
        for word in ("done", "blocked"):
            with self.subTest(word):
                fake = Tmux(pane="w1")
                tmux_host.mark(word, self.events, pane="%2", tmux=TMUX, proc=fake)
                self.assertEqual(
                    fake.calls,
                    [
                        [TMUX, "set-option", "-t", "%2", "@state", word],
                        [TMUX, "display-message", "-p", "-t", "%2", "#S"],
                    ],
                )
                self.assertEqual(fake.kwargs, [MARK_KW] * 2)
        self.assertRegex(self.text(), r"^old\n\d\d:\d\d:\d\d w1 done\n\d\d:\d\d:\d\d w1 blocked\n$")

    def test_no_events_file_sets_the_state_only(self):
        for word in ("done", "blocked"):
            with self.subTest(word):
                fake = Tmux(pane="w1")
                tmux_host.mark(word, None, proc=fake)
                self.assertEqual(fake.calls, [["tmux", "set-option", "-t", "%1", "@state", word]])
        self.assertEqual(self.text(), "old\n")

    def test_its_pane_is_tmux_pane_else_nothing_is_marked(self):
        fake = Tmux(pane="w1")
        tmux_host.mark("done", self.events, proc=fake)
        self.assertEqual([c[c.index("-t") + 1] for c in fake.calls], ["%1", "%1"])
        for env in ({}, {"TMUX_PANE": ""}):
            with self.subTest(env=env), environ(**env):
                fake = Tmux(pane="w1")
                tmux_host.mark("done", self.events, proc=fake)
                self.assertEqual(fake.calls, [])
        self.assertEqual(len(self.text().splitlines()), 2)

    def test_refuses_an_events_file_not_a_regular_file_of_the_callers(self):
        link = os.path.join(self.root, "link")
        os.symlink(self.events, link)
        fifo = unittest.mock.Mock(st_mode=stat.S_IFIFO | 0o600, st_uid=os.getuid())
        for path, patches, error in (
            (link, (), ""),
            (self.root, (), ""),
            (os.path.join(self.root, "missing"), (), "No such file"),
            (self.events, ("os.getuid", os.getuid() + 1), "owned by you"),
            (self.events, ("os.fstat", fifo), "owned by you"),
        ):
            with (
                self.subTest(path=path, error=error),
                ExitStack() as stack,
                self.assertRaisesRegex(
                    tmux_host.TuiError, f"^events file {re.escape(path)}: .*{error}"
                ),
            ):
                if patches:
                    stack.enter_context(unittest.mock.patch(patches[0], return_value=patches[1]))
                tmux_host.mark("done", path, proc=Tmux(pane="w1"))
        self.assertEqual(self.text(), "old\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "missing")))

    def test_a_failed_tmux_call_or_bad_value_raises_writing_no_line(self):
        timeout = TimeoutExpired(["tmux"], 5)
        cases = (
            (Tmux(pane="w1", fail={"set-option"}), {}, "^tmux: boom$"),
            (Tmux(), {}, "^tmux: no server running$"),
            (Tmux(results={"set-option": timeout}), {}, "^tmux: timed out after 5 s$"),
            (
                Tmux(results={"display-message": FileNotFoundError(2, "No such file")}),
                {},
                "^tmux: ",
            ),
            (Tmux(pane="a b"), {}, "^tmux session 'a b'"),
            (Tmux(pane="w1"), {"pane": "w1:0"}, "^bad tmux pane 'w1:0'$"),
        )
        for fake, kw, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(tmux_host.TuiError, error):
                tmux_host.mark("done", self.events, proc=fake, **kw)
        self.assertEqual(self.text(), "old\n")


class EventsFileTest(Case):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)

    def test_created_0600_and_made_absolute(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        path = os.path.join(self.root, "e")
        self.assertEqual(tmux_host.events_file("e"), path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, "a") as f:
            f.write("x\n")
        self.assertEqual(tmux_host.events_file(path), path)
        with open(path) as f:
            self.assertEqual(f.read(), "x\n")

    def test_refused(self):
        target = os.path.join(self.root, "target")
        open(target, "w").close()
        link = os.path.join(self.root, "link")
        os.symlink(target, link)
        for path in (link, self.root, os.path.join(self.root, "missing", "e")):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(tmux_host.TuiError, f"^events file {re.escape(path)}: "),
            ):
                tmux_host.events_file(path)
        fifo = unittest.mock.Mock(st_mode=stat.S_IFIFO | 0o600, st_uid=os.getuid())
        for patch in (
            unittest.mock.patch("os.getuid", return_value=os.getuid() + 1),
            unittest.mock.patch("os.fstat", return_value=fifo),
        ):
            with (
                self.subTest(patch=patch),
                patch,
                self.assertRaisesRegex(tmux_host.TuiError, "owned by you"),
            ):
                tmux_host.events_file(target)

    def test_a_fifo_without_a_reader_is_refused_at_once(self):
        fifo = os.path.join(self.root, "fifo")
        os.mkfifo(fifo, 0o600)
        caught = []

        def run():
            try:
                tmux_host.events_file(fifo)
            except TuiError as e:
                caught.append(e)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(5)
        if thread.is_alive():
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # lets the blocked open return
            thread.join()
            os.close(reader)
            self.fail("events_file blocked on a FIFO without a reader")
        self.assertRegex(str(caught[0]), f"^events file {re.escape(fifo)}: ")

    def test_refuses_a_path_tmux_would_misread(self):
        for name in ("a#b", "e;"):
            path = os.path.join(self.root, name)
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    tmux_host.TuiError, f"^events file {re.escape(path)}: tmux would misread"
                ),
            ):
                tmux_host.events_file(path)
            self.assertFalse(os.path.exists(path))


class DisplayErrorTest(Case):
    def test_none_or_printable_text_not_ending_in_a_semicolon(self):
        for value in (None, "", "mgr", "mgr/right/m1", "elsewhere:1.2", "a b#{x}", "\u00e9"):
            with self.subTest(value=value):
                self.assertIsNone(tmux_host.display_error(value))
        for value in ("a\tb", "a\nb", "a\0b", "a\u2028b", "mgr;", 5, b"x"):
            with self.subTest(value=value):
                self.assertEqual(
                    tmux_host.display_error(value),
                    f"display {value!r:.80}: want printable text not ending in ;",
                )


class ProcessIdentityTest(Case):
    FORMAT = "#{pid} #{session_id} #{pane_id} #{pane_pid}"

    def test_the_server_session_pane_and_process_ids_of_its_pane_else_none(self):
        for target, printed, want in (
            ("s", (0, "4242 $3 %7 4343\n"), "4242 $3 %7 4343"),
            ("%7", (0, "4242 $3 %7 4343\n"), "4242 $3 %7 4343"),
            ("s", (0, " \n"), None),
            ("%7", (0, "4242   \n"), None),
            ("s", (1, "", "can't find session: =s\n"), None),
            ("%7", (1, "", "can't find pane: %7\n"), None),
            ("s", (1, "", "no server running on /tmp/tmux-501/default\n"), None),
        ):
            with self.subTest(target=target, printed=printed):
                fake = Tmux(results={"display-message": printed})
                self.assertEqual(tmux_host.process(target, proc=fake), want)
                at = "=s:" if target == "s" else target
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", at, self.FORMAT]]
                )

    def test_a_failure_bad_output_or_bad_target_raises(self):
        for printed, error in (
            ((1, "", "boom\n"), "^tmux: boom$"),
            ((0, "4242 $3 %7\n"), "^bad tmux process "),
        ):
            with self.subTest(printed=printed):
                fake = Tmux(results={"display-message": printed})
                with self.assertRaisesRegex(tmux_host.TuiError, error):
                    tmux_host.process("s", proc=fake)
        fake = Tmux()
        with self.assertRaisesRegex(tmux_host.TuiError, "invalid target"):
            tmux_host.process("a b", proc=fake)
        self.assertEqual(fake.calls, [])


class StatusTest(Case):
    def test_states(self):
        # a signal death: -n (tmux prints the signal's name where libc has one, else its number)
        for pane, want in (
            ("0  ", tmux_host.PaneStatus.RUNNING),
            ("1 3 ", 3),
            ("1 0 ", 0),
            ("1  kill", -9),
            ("1  9", -9),
            ("1  term", -15),
            ("1  ", tmux_host.PaneStatus.RUNNING),
            ("  ", None),
            (None, None),
        ):
            with self.subTest(pane=pane):
                fake = Tmux(pane=pane)
                self.assertEqual(tmux_host.status("s", proc=fake), want)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_a_pane_status_is_a_str_enum_member(self):
        self.assertEqual(list(tmux_host.PaneStatus), ["running", "unknown"])
        for name in ("RUNNING", "UNKNOWN"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(tmux_host, name))
        for result, want in (
            ((0, "0  \n"), tmux_host.PaneStatus.RUNNING),
            ((0, "x\n"), tmux_host.PaneStatus.UNKNOWN),
        ):
            with self.subTest(result=result):
                fake = Tmux(results={"display-message": result})
                self.assertIs(tmux_host.status("s", proc=fake), want)

    def test_absent_for_a_missing_session_or_server_else_unknown(self):
        missing = FileNotFoundError(2, "No such file or directory", "tmux")
        for result, want in (
            *(((1, "", err + "\n"), None) for err in ABSENT),
            *(((1, "", err + "\n"), tmux_host.PaneStatus.UNKNOWN) for err in FAILED),
            ((0, "x\n"), tmux_host.PaneStatus.UNKNOWN),
            ((0, "2 0 \n"), tmux_host.PaneStatus.UNKNOWN),
            ((0, "1 0\n"), tmux_host.PaneStatus.UNKNOWN),
            ((0, ""), tmux_host.PaneStatus.UNKNOWN),
            (missing, tmux_host.PaneStatus.UNKNOWN),
            (PermissionError(13, "Permission denied"), tmux_host.PaneStatus.UNKNOWN),
        ):
            with self.subTest(result=result):
                fake = Tmux(results={"display-message": result})
                self.assertEqual(tmux_host.status("s", proc=fake), want)
                self.assertEqual(len(fake.calls), 1)


class TmuxPathTest(Case):
    def test_the_real_path_of_the_tmux_on_path_else_none(self):
        with tempfile.TemporaryDirectory() as d:
            real, bin_dir = os.path.join(d, "real"), os.path.join(d, "bin")
            os.mkdir(real)
            os.mkdir(bin_dir)
            exe = os.path.join(real, "tmux-3.7")
            with open(exe, "w"):
                pass
            os.chmod(exe, 0o755)
            os.symlink(exe, os.path.join(bin_dir, "tmux"))
            with environ(PATH=bin_dir):
                self.assertEqual(
                    tmux_host.tmux_path(), os.path.join(os.path.realpath(d), "real", "tmux-3.7")
                )
            for path in (real, ""):
                with self.subTest(path=path), environ(PATH=path):
                    self.assertIsNone(tmux_host.tmux_path())


class KillTest(Case):
    def test_kill(self):
        fake = Tmux()
        tmux_host.kill("s", proc=fake)
        self.assertEqual(fake.calls, [["tmux", "kill-session", "-t", "=s"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_failure(self):
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            tmux_host.kill("s", proc=Tmux(fail={"kill-session"}))


class GridOpenTest(Case):
    """Grid mode. The caller is in root session mgr (no @opener), pane %3, shown by a terminal
    client (tty /dev/ttys004, its pane %9); `windows` maps a pane to its window's list-panes rows
    (window, @grid-manager, pane, width, height), a pane not in it is gone; list-panes -a prints
    nothing; iTerm2 runs; a split prints %20; tile records its call in the fake's calls.
    """

    MGR = ("$1", "mgr", "", "")

    NEW = [("@1", "", "%3", "160", "48")]  # mgr's window, no grid yet

    NOTICE = "tui: show: grid: --split/--split-from ignored\n"

    def setUp(self):
        self.tile = unittest.mock.Mock(
            side_effect=lambda window, *, per_column, proc: proc.calls.append(
                ["tile", window, per_column]
            )
        )
        for p in (unittest.mock.patch.object(sys, "executable", PY),):
            p.start()
            self.addCleanup(p.stop)

    def fake(
        self, windows, rows=(MGR,), own="mgr", clients=f"5 /dev/ttys004 %9 0 {SOCK}\n", **results
    ):
        def list_panes(argv):
            if "-a" in argv:
                return 0, ""
            if argv[3] not in windows:
                return 1, "", f"can't find pane: {argv[3]}\n"
            return 0, "".join("\t".join((*r, SOCK)) + "\n" for r in windows[argv[3]])

        return Tmux(
            results={
                "display-message": (0, own + "\n"),
                "list-sessions": (0, "".join("\t".join((*r, SOCK)) + "\n" for r in rows)),
                "list-clients": (0, clients),
                "list-panes": list_panes,
                "split-window": (0, "%20\n"),
                "pgrep": (0,),
                "osascript": (0, "ok NEW\n"),
                **results,
            }
        )

    def show(self, fake, env=INSIDE, retile="all", **kw):
        """show s; retile all unless given, the mode whose hooks opened() asserts by default."""
        self.stderr = io.StringIO()
        script = kw.pop("script", SCRIPT)
        with environ(**env), which(), redirect_stderr(self.stderr):
            return tmux_host.show(
                "s", retile=retile, **kw, proc=fake, tile_window=self.tile, script=script
            )

    def split(self, flag, pane, window="@1"):
        """A grid split: the window's hooks muted first, so a previous mode's hook starts no tile;
        grid_set unmutes.
        """
        return [mute(window), split_window(flag, pane)]

    def test_own_root_starts_a_grid_right_of_its_pane(self):
        # plain tmux in iTerm2 ($ITERM_SESSION_ID set, iTerm2 running): a tmux split all the same
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("mgr"),
                grid_read("%3"),
                *self.split("-h", "%3"),
                *opening_commands(),
            ],
        )
        self.assertEqual(self.stderr.getvalue(), ATTACH)
        # its @pane gone: still a root
        fake = self.fake({"%3": self.NEW}, [("$1", "mgr", "", "%77")])
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                grid_read("%77"),
                clients_of("mgr"),
                grid_read("%3"),
                *self.split("-h", "%3"),
                *opening_commands(),
            ],
        )

    def test_the_largest_pane_but_the_managers_splits_below(self):
        for workers, target in (
            ([("%10", "80", "24"), ("%11", "80", "23")], "%10"),
            ([("%12", "40", "48"), ("%8", "80", "24"), ("%4", "20", "10")], "%12"),
            ([("%8", "80", "24"), ("%12", "40", "48")], "%12"),
        ):
            with self.subTest(target=target, workers=workers):
                fake = self.fake(
                    {"%3": [("@1", "%3", "%3", "79", "48"), *(("@1", "%3", *w) for w in workers)]}
                )
                self.assertIsNone(self.show(fake))
                self.assertEqual(
                    fake.calls[3:],
                    [grid_read("%3"), *self.split("-v", target), *opening_commands()],
                )

    def test_a_stored_manager_not_a_pane_of_the_window_starts_a_new_grid_at_the_anchor(self):
        for manager in ("%99", "x", "%3;", " %3"):
            with self.subTest(manager=manager):
                fake = self.fake(
                    {"%3": [("@1", manager, "%3", "79", "48"), ("@1", manager, "%5", "80", "48")]}
                )
                self.assertIsNone(self.show(fake))
                self.assertEqual(
                    fake.calls[3:], [grid_read("%3"), *self.split("-v", "%5"), *opening_commands()]
                )

    def test_a_root_not_the_callers_anchors_at_its_clients_pane(self):
        # the most recently active client's pane; a control-mode client (iTerm2's tmux -CC) too
        clients = f"5 /dev/ttys004 %8 0 {SOCK}\n9 /dev/ttys009 %9 1 {SOCK}\n"
        for env, head in (
            (INSIDE, [SESSIONS, clients_of("mgr"), OWN]),
            ({}, [SESSIONS, clients_of("mgr")]),
        ):
            with self.subTest(env=env):
                fake = self.fake(
                    {"%9": [("@2", "", "%9", "160", "48")]}, own="drv", clients=clients
                )
                self.assertIsNone(self.show(fake, env, opener="mgr"))
                self.assertEqual(
                    fake.calls,
                    [
                        *head,
                        grid_read("%9"),
                        *self.split("-h", "%9", "@2"),
                        *opening_commands(window="@2", manager="%9"),
                    ],
                )
        # the client's pane in a grid window: that grid
        fake = self.fake(
            {"%9": [("@1", "%3", "%3", "79", "48"), ("@1", "%3", "%9", "80", "48")]}, own="drv"
        )
        self.assertIsNone(self.show(fake, opener="mgr"))
        self.assertEqual(
            fake.calls[3:], [grid_read("%9"), *self.split("-v", "%9"), *opening_commands()]
        )

    def test_a_given_opener_that_is_the_callers_session_anchors_at_its_pane(self):
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.show(fake, opener="mgr"))
        self.assertEqual(
            fake.calls,
            [
                SESSIONS,
                clients_of("mgr"),
                OWN,
                grid_read("%3"),
                *self.split("-h", "%3"),
                *opening_commands(),
            ],
        )

    def test_a_member_opens_a_sub_worker_in_its_window(self):
        rows = (self.MGR, ("$2", "w1", "mgr", "%10"))
        window = [("@1", "%3", "%3", "79", "48"), ("@1", "%3", "%10", "80", "48")]
        for kw, head in (({}, [OWN, SESSIONS]), ({"opener": "w1"}, [SESSIONS])):
            with self.subTest(**kw):
                fake = self.fake({"%10": window}, rows, own="w1")
                self.assertIsNone(self.show(fake, **kw))
                self.assertEqual(
                    fake.calls,
                    [
                        *head,
                        grid_read("%10"),
                        *self.split("-v", "%10"),
                        *opening_commands(opener="w1"),
                    ],
                )

    def test_todays_path_for_a_detached_root_or_a_member_outside_a_grid(self):
        fake = self.fake({"%3": self.NEW}, clients="")
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("mgr"),
                clients_of("mgr"),
                PGREP,
                osa("right", "id", "s", "ABC"),
                *record("mgr", "NEW"),
            ],
        )
        rows = (self.MGR, ("$2", "w1", "x", "%10"))
        for windows in (
            {"%10": [("@1", "", "%10", "160", "48")]},
            {"%10": [("@1", "%9", "%10", "160", "48")]},
            {},
        ):
            with self.subTest(windows=windows):
                fake = self.fake(windows, rows, own="w1", pgrep=(1,))
                self.assertIsNone(self.show(fake))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        grid_read("%10"),
                        clients_of("w1"),
                        HOSTS,
                        PGREP,
                        split_window("-h", "%3"),
                        *record("w1", "%20"),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_a_failed_or_unreadable_grid_read_falls_back_to_todays_path(self):
        for windows in (
            {},
            *(
                {"%3": [row]}
                for row in (
                    ("@1;", "", "%3", "160", "48"),
                    ("1", "", "%3", "160", "48"),
                    ("@1", "", "%3;", "160", "48"),
                    ("@1", "", "%3", "x", "48"),
                    ("@1", "", "%3", "160", "²"),
                    ("@1", "", "%3", "160"),
                )
            ),
        ):
            with self.subTest(windows=windows):
                fake = self.fake(windows, pgrep=(1,))
                self.assertIsNone(self.show(fake))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of("mgr"),
                        grid_read("%3"),
                        clients_of("mgr"),
                        HOSTS,
                        PGREP,
                        split_window("-h", "%3"),
                        *record("mgr", "%20"),
                    ],
                )
        fake = self.fake({"%3": self.NEW}, **{"list-clients": (1, "", "boom\n")})
        self.assertEqual(self.show(fake), "tmux: boom" + WATCH)
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("mgr"), clients_of("mgr")])

    def test_split_and_split_from_ignored_in_the_grid(self):
        for kw in ({"split": "below"}, {"split_from": "b"}, {"split": "right", "split_from": "b"}):
            with self.subTest(**kw):
                fake = self.fake({"%3": self.NEW})
                self.assertIsNone(self.show(fake, **kw))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of("mgr"),
                        grid_read("%3"),
                        *self.split("-h", "%3"),
                        *opening_commands(),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), ATTACH + self.NOTICE)

    def test_split_honored_outside_the_grid(self):
        fake = self.fake({"%3": self.NEW}, clients="")
        self.assertIsNone(self.show(fake, split="below"))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("mgr"),
                clients_of("mgr"),
                PGREP,
                osa("below", "id", "s", "ABC"),
                *record("mgr", "NEW"),
            ],
        )
        self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_a_failed_split_changes_nothing_else(self):
        # the hooks unmuted again, after a split that raises too
        fake = self.fake({"%3": self.NEW}, **{"split-window": (1, "", "no space for new pane\n")})
        self.assertEqual(self.show(fake), "tmux: no space for new pane")
        self.assertEqual(fake.calls[3:], [grid_read("%3"), *self.split("-h", "%3"), unmute("@1")])
        self.assertEqual(
            self.stderr.getvalue(), ATTACH + "tui: show: tmux: no space for new pane\n"
        )
        fake = self.fake({}, **{"list-panes": lambda _argv: (0, "@1\t\t%3\t160\t48\t/tmp/a#b\n")})
        self.assertEqual(self.show(fake), "tmux would misread '/tmp/a#b'" + WATCH)
        self.assertEqual(fake.calls[3:], [grid_read("%3"), mute("@1"), unmute("@1")])

    def test_hooks_need_paths_tmux_takes_verbatim(self):
        for target, name, bad in (
            (sys, "executable", "/a b/python3"),
            (sys, "executable", "/a#b/python3"),
            (sys, "executable", "/a/python3;"),
            (sys, "executable", "/a/pythön"),
            (sys, "executable", ""),
            (sys, "executable", None),
            (None, "script", "/a'b/tmux_host.py"),
            (None, "script", "/a$(x)/tmux_host.py"),
        ):
            with (
                self.subTest(name=name, bad=bad),
                (
                    unittest.mock.patch.object(target, name, bad)
                    if target is not None
                    else contextlib.nullcontext()
                ),
            ):
                fake = self.fake({"%3": self.NEW})
                self.assertIsNone(self.show(fake, **({"script": bad} if target is None else {})))
                self.assertEqual(
                    fake.calls[-4:],
                    [*record("mgr", "%20"), grid_set("@1", "%3", hooks=False), ["tile", "@1", 3]],
                )
                self.assertEqual(
                    self.stderr.getvalue(),
                    ATTACH + f"tui: show: grid: hooks: {bad or ''}: tmux would misread it\n",
                )

    def test_retile_modes(self):
        # None: the default, open-close
        for retile, mode in ((None, "open-close"), *((m, m) for m in ("all", "open-close", "off"))):
            with self.subTest(retile=retile):
                fake = self.fake({"%3": self.NEW})
                self.assertIsNone(self.show(fake, retile=retile))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of("mgr"),
                        grid_read("%3"),
                        *self.split("-h", "%3"),
                        *opening_commands(retile=mode),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_retile_modes_with_a_hook_path_tmux_would_misread(self):
        # all and open-close: the notice, no hook set, the others unset; off: no hook to set, no
        # notice
        notice = "tui: show: grid: hooks: /a b/python3: tmux would misread it\n"
        with unittest.mock.patch.object(sys, "executable", "/a b/python3"):
            for retile, printed in (("all", notice), ("open-close", notice), ("off", "")):
                with self.subTest(retile=retile):
                    fake = self.fake({"%3": self.NEW})
                    self.assertIsNone(self.show(fake, retile=retile))
                    self.assertEqual(fake.calls[6:], opening_commands(retile=retile, hooks=False))
                    self.assertEqual(self.stderr.getvalue(), ATTACH + printed)

    def test_bad_retile_raises_before_tmux(self):
        for bad in ("x", True, 1, ""):
            with self.subTest(retile=bad):
                fake = self.fake({"%3": self.NEW})
                for call in (
                    lambda *, bad=bad, fake=fake: self.show(fake, retile=bad),
                    lambda *, bad=bad, fake=fake: tmux_host.open_pane(
                        "s", retile=bad, proc=fake, script=tmux_host.__file__
                    ),
                ):
                    with self.assertRaisesRegex(
                        tmux_host.TuiError, RETILE_ERROR.format(re.escape(repr(bad)))
                    ):
                        call()
                self.assertEqual(fake.calls, [])

    def test_a_failure_after_the_split_is_the_shows_and_leaves_the_pane(self):
        fake = self.fake(
            {"%3": self.NEW},
            **{"set-option": lambda argv: (1, "", "boom\n") if "-w" in argv else (0,)},
        )
        self.assertEqual(self.show(fake), "tmux: boom")
        self.assertEqual(fake.calls[-3:], [*record("mgr", "%20"), grid_set("@1", "%3")])
        self.tile.side_effect = tmux_host.TuiError("tmux: boom")
        fake = self.fake({"%3": self.NEW})
        self.assertEqual(self.show(fake), "tmux: boom")
        self.assertEqual(fake.calls[-3:], [*record("mgr", "%20"), grid_set("@1", "%3")])
        self.assertNotIn("kill-session", fake.commands())
        self.assertEqual(self.stderr.getvalue(), ATTACH + "tui: show: tmux: boom\n")

    def test_per_column(self):
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.show(fake, per_column=2))
        self.assertEqual(fake.calls[-2:], [grid_set("@1", "%3", 2), ["tile", "@1", 2]])
        for bad in (0, 10000, -1, True, "3", 2.0):
            with self.subTest(per_column=bad):
                fake = self.fake({"%3": self.NEW})
                for call in (
                    lambda *, bad=bad, fake=fake: self.show(fake, per_column=bad),
                    lambda *, bad=bad, fake=fake: tmux_host.open_pane(
                        "s", per_column=bad, proc=fake, script=tmux_host.__file__
                    ),
                ):
                    with self.assertRaisesRegex(
                        tmux_host.TuiError, "^per_column must be an int from 1 to 9999"
                    ):
                        call()
                self.assertEqual(fake.calls, [])


class RetileTest(Case):
    def test_modes(self):
        self.assertEqual(list(tmux_host.Retile), ["all", "open-close", "off"])
        self.assertIs(tmux_host.RETILE, tmux_host.Retile.OPEN_CLOSE)
        self.assertEqual(tmux_host.RETILE_HOOKS, MODE_HOOKS)
        self.assertEqual(list(tmux_host.RETILE_HOOKS), list(tmux_host.Retile))
        for retile in tmux_host.RETILE_HOOKS:
            self.assertIs(type(retile), tmux_host.Retile)
        self.assertEqual(tmux_host.GRID_PANES_SET, "@grid-panes")
        for retile in (None, *tmux_host.Retile, "all"):
            with self.subTest(retile=retile):
                self.assertIsNone(tmux_host._retile(retile))
        for bad in ("x", True, 1, "", "ALL"):
            with (
                self.subTest(retile=bad),
                self.assertRaisesRegex(
                    tmux_host.TuiError, RETILE_ERROR.format(re.escape(repr(bad)))
                ),
            ):
                tmux_host._retile(bad)


class SplitTest(Case):
    def test_sides(self):
        self.assertEqual(list(tmux_host.Split), ["right", "below"])
        for split in (None, *tmux_host.Split, "below"):
            with self.subTest(split=split):
                self.assertIsNone(tmux_host._layout(split, None))
        for bad in ("up", "", "RIGHT", 1, ["right"]):
            with (
                self.subTest(split=bad),
                self.assertRaisesRegex(
                    tmux_host.TuiError,
                    f"^split must be one of right, below, not {re.escape(repr(bad))}$",
                ),
            ):
                tmux_host._layout(bad, None)


class GridTreeTest(Case):
    """Manager %0, its session mgr; rows: (session id, name, @opener, @pane)."""

    MGR = (1, "mgr", "", "")

    def tree(self, rows, panes, layout=None):
        if layout is None:
            layout = grid([slot(p) for p in panes if p != "%0"], per_column=len(panes))
        else:
            layout = tmux_host._parse_layout(layout)
        return tmux_host._grid_tree("%0", panes, [self.MGR, *rows], layout)

    def test_cells_by_session_name_sub_workers_in_their_parents_cell(self):
        names = [
            "pm-TASK-10-a",
            "engineer-TASK-227-b",
            "researcher-TASK-3-c",
            "engineer-TASK-23-d",
            "pm-TASK-9-e",
            "engineer-TASK-9-f",
        ]
        for case, rows, panes, want in (
            (
                "nodes by session name, then manual panes",
                [(2, "a", "mgr", "%10"), (3, "b", "mgr", "%2"), (4, "c", "", "%9")],
                ["%0", "%10", "%5", "%2", "%9", "%3"],
                [slot("%10"), slot("%2"), slot("%9"), slot("%3"), slot("%5")],
            ),
            (
                "main cells by role, then numbers as numbers",
                [(i + 2, name, "mgr", f"%{i + 1}") for i, name in enumerate(names)],
                ["%0", "%1", "%2", "%3", "%4", "%5", "%6"],
                [slot(p) for p in ("%6", "%4", "%2", "%5", "%1", "%3")],
            ),
            (
                "sub-workers by pane id",
                [
                    (2, "pm-1", "mgr", "%1"),
                    (3, "engineer-2", "mgr", "%2"),
                    (4, "z", "pm-1", "%3"),
                    (5, "a", "pm-1", "%4"),
                    (6, "b", "engineer-2", "%5"),
                ],
                ["%0", "%1", "%2", "%3", "%4", "%5"],
                [slot("%2", slot("%5")), slot("%1", slot("%3"), slot("%4"))],
            ),
            (
                "a grandchild in its parent's cell",
                [
                    (2, "w1", "mgr", "%1"),
                    (3, "w2", "mgr", "%2"),
                    (4, "a", "w2", "%3"),
                    (5, "b", "w2", "%4"),
                    (6, "w3", "mgr", "%5"),
                    (7, "c", "a", "%6"),
                ],
                ["%0", "%1", "%2", "%3", "%4", "%5", "%6"],
                [slot("%1"), slot("%2", slot("%3", slot("%6")), slot("%4")), slot("%5")],
            ),
        ):
            with self.subTest(case):
                self.assertEqual(self.tree(rows, panes), want)

    def test_a_driver_with_an_opener_and_no_pane_takes_no_cell(self):
        rows = [
            (2, "w1", "mgr", "%1"),
            (3, "r-0123abcd-drive", "mgr", ""),
            (4, "r-0123abcd", "mgr", "%2"),
        ]
        self.assertEqual(self.tree(rows, ["%0", "%1", "%2"]), [slot("%2"), slot("%1")])

    def test_a_closed_pane_keeps_the_rest_in_order(self):
        panes = ["%0", "%1", "%3", "%4", "%5", "%6", "%7"]
        rows = [(i + 1, f"w{i}", "mgr", f"%{i}") for i in (1, 3, 4, 5, 6, 7)]
        cells = self.tree(rows, panes)
        self.assertEqual(cells, [slot(p) for p in panes[1:]])
        self.assertEqual(
            [[c.pane for c in col.children] for col in grid(cells).children[1].children],
            [["%1", "%3", "%4"], ["%5", "%6", "%7"]],
        )

    def test_openers_that_are_no_node_of_the_window(self):
        rows = [
            (2, "x", "mgr", "%77"),
            (3, "y", "x", "%1"),
            (4, "z", "mgr", "%0"),
            (5, "v", "z", "%2"),
            (6, "u", "", "%3"),
        ]
        self.assertEqual(
            self.tree(rows, ["%0", "%1", "%2", "%3"]), [slot("%3"), slot("%2"), slot("%1")]
        )

    def test_duplicate_pane_claims_the_lower_session_id_owns(self):
        rows = [
            (5, "x", "mgr", "%1"),
            (3, "y", "mgr", "%1"),
            (6, "z", "x", "%2"),
            (7, "w", "y", "%3"),
        ]
        self.assertEqual(
            self.tree(rows, ["%0", "%1", "%2", "%3"]), [slot("%1", slot("%3")), slot("%2")]
        )

    def test_opener_cycle_goes_main(self):
        rows = [(2, "a", "b", "%1"), (3, "b", "a", "%2"), (4, "c", "a", "%3"), (5, "d", "d", "%4")]
        self.assertEqual(
            self.tree(rows, ["%0", "%1", "%2", "%3", "%4"]),
            [slot("%1"), slot("%2"), slot("%3"), slot("%4")],
        )

    def test_an_orphan_group_main_does_not_reach_goes_main_whole(self):
        # a (%1) and lead (%2) open each other; the orphans of gone c1 (%3, %4) sit right of a, or
        # of x (%5, a's)
        cycle = [
            (2, "a", "lead", "%1"),
            (3, "lead", "a", "%2"),
            (4, "k1", "c1", "%3"),
            (5, "k2", "c1", "%4"),
        ]
        for rows, panes, layout, want in (
            (
                cycle,
                ["%0", "%1", "%2", "%3", "%4"],
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0{40x24,80,0,1,39x24,121,0[39x12,121,0,3,39x11,121,13,4]},"
                    "80x23,80,25,2]}"
                ),
                [slot("%1"), slot(None, slot("%3"), slot("%4")), slot("%2")],
            ),
            (
                [*cycle, (6, "x", "a", "%5")],
                ["%0", "%1", "%2", "%3", "%4", "%5"],
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0{40x16,80,0,5,39x16,121,0[39x8,121,0,3,39x7,121,9,4]},"
                    "80x15,80,17,1,80x15,80,33,2]}"
                ),
                [slot("%1"), slot(None, slot("%3"), slot("%4")), slot("%2"), slot("%5")],
            ),
        ):
            with self.subTest(panes=panes):
                self.assertEqual(self.tree(rows, panes, layout), want)

    def test_orphans_take_the_killed_parents_cell(self):
        orphans = slot(None, slot("%4"), slot("%5"))
        for case, layout, rows, panes, want in (
            # w2 (%2, with a %4 and b %5) killed: tmux gave its cell to [a, b]
            (
                "main cell",
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17[80x7,80,17,4,80x7,80,25,5],80x15,80,33,3]}"
                ),
                [
                    (2, "w1", "mgr", "%1"),
                    (4, "w3", "mgr", "%3"),
                    (5, "a", "w2", "%4"),
                    (6, "b", "w2", "%5"),
                ],
                ["%0", "%1", "%3", "%4", "%5"],
                [slot("%1"), orphans, slot("%3")],
            ),
            # pm-1 (with a %4 and b %5) killed, its orphans' cell last
            (
                "place by name",
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17,3,80x15,80,33[80x7,80,33,4,80x7,80,41,5]]}"
                ),
                [
                    (2, "engineer-1", "mgr", "%1"),
                    (4, "researcher-1", "mgr", "%3"),
                    (5, "a", "pm-1", "%4"),
                    (6, "b", "pm-1", "%5"),
                ],
                ["%0", "%1", "%3", "%4", "%5"],
                [slot("%1"), orphans, slot("%3")],
            ),
            # w1 (%1) has children c (%2, killed; its children a %4, b %5) and d (%3)
            (
                "sub cell",
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0,1,39x48,121,0[39x24,121,0[39x12,121,0,4,"
                    "39x11,121,13,5],39x23,121,25,3]}}"
                ),
                [
                    (2, "w1", "mgr", "%1"),
                    (4, "d", "w1", "%3"),
                    (5, "a", "c", "%4"),
                    (6, "b", "c", "%5"),
                ],
                ["%0", "%1", "%3", "%4", "%5"],
                [slot("%1", orphans, slot("%3"))],
            ),
            # w2 killed as in the main cell case, manual pane %9 beside its orphans: they skip %9's
            # cell
            (
                "skipping a manual pane's cell",
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17{40x15,80,17,9,39x15,121,17[39x7,121,17,4,"
                    "39x7,121,25,5]},80x15,80,33,3]}"
                ),
                [
                    (2, "w1", "mgr", "%1"),
                    (4, "w3", "mgr", "%3"),
                    (5, "a", "w2", "%4"),
                    (6, "b", "w2", "%5"),
                ],
                ["%0", "%1", "%3", "%4", "%5", "%9"],
                [slot("%1"), orphans, slot("%3"), slot("%9")],
            ),
        ):
            with self.subTest(case):
                self.assertEqual(self.tree(rows, panes, layout), want)

    def test_orphans_beside_a_one_pane_first_column_stay_main(self):
        # P (%2; c1 %3, c2 %4) killed. N=1: main [a, P, b]; N=2: main [a, x, P, b], x and P killed
        # together
        rows = [
            (2, "a", "mgr", "%1"),
            (4, "c1", "P", "%3"),
            (5, "c2", "P", "%4"),
            (6, "b", "mgr", "%5"),
        ]
        orphans = slot(None, slot("%3"), slot("%4"))
        for layout in (
            tmux_host._render_layout(grid([slot("%1"), orphans, slot("%5")], 1)),
            "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0,1,39x48,121,0[39x24,121,0[39x12,121,0,3,"
            "39x11,121,13,4],39x23,121,25,5]}}",
        ):
            with self.subTest(layout=layout):
                self.assertEqual(
                    self.tree(rows, ["%0", "%1", "%3", "%4", "%5"], layout),
                    [orphans, slot("%1"), slot("%5")],
                )

    def test_two_orphan_groups_beside_a_one_pane_first_column_stay_main(self):
        # N=1: main [a, P1 (c1 %3, c2 %4), P2 (d1 %6, d2 %7)], both killed; either group resolved
        # first
        groups = slot(None, slot("%3"), slot("%4")), slot(None, slot("%6"), slot("%7"))
        layout = tmux_host._render_layout(grid([slot("%1"), *groups], 1))
        for p1, p2, want in (("p", "q", groups), ("q", "p", groups[::-1])):
            with self.subTest(p1=p1, p2=p2):
                rows = [
                    (2, "a", "mgr", "%1"),
                    (4, "c1", p1, "%3"),
                    (5, "c2", p1, "%4"),
                    (7, "d1", p2, "%6"),
                    (8, "d2", p2, "%7"),
                ]
                self.assertEqual(
                    self.tree(rows, ["%0", "%1", "%3", "%4", "%6", "%7"], layout),
                    [slot("%1"), *want],
                )

    def test_orphans_spanning_main_cells_dissolve(self):
        # the manager's session renamed from old to mgr
        for panes, per_column in (
            (["%0", "%1", "%2", "%3"], 3),
            (["%0", "%1", "%2", "%3"], 2),
            (["%0", "%1"], 3),
        ):
            with self.subTest(panes=panes, per_column=per_column):
                layout = grid([slot(p) for p in panes[1:]], per_column)
                rows = [(int(p[1:]) + 1, f"w{p[1:]}", "old", p) for p in panes[1:]]
                self.assertEqual(
                    self.tree(rows, panes, tmux_host._render_layout(layout)),
                    [slot(p) for p in panes[1:]],
                )


class SendTest(Case):
    def send(self, fake, text):
        tmux_host.send(
            "s", text, proc=fake, sleep=lambda seconds: fake.calls.append(("sleep", seconds))
        )
        buffer = fake.calls[0][3]
        self.assertRegex(buffer, r"^tui-s-[0-9a-f]{32}$")
        return buffer

    def test_pastes_the_text_then_presses_enter(self):
        fake = Tmux()
        buffer = self.send(fake, "-x hi")
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", "=s:"],
                ("sleep", 0.5),
                ["tmux", "send-keys", "-t", "=s:", "Enter"],
            ],
        )
        self.assertEqual(
            fake.kwargs,
            [{"capture_output": True, "text": True, "input": "-x hi"}, TMUX_KW, TMUX_KW],
        )

    def test_text_goes_through_stdin_verbatim(self):
        long_lines = "\n".join(f"line {i:02d} " + "x" * 50 for i in range(28))
        for text in ("ls;", ";", "a\\;", "a;b", long_lines, "one\ntwo;\n"):
            with self.subTest(text=text[:20]):
                fake = Tmux()
                self.send(fake, text)
                self.assertEqual(fake.kwargs[0]["input"], text)
                self.assertNotIn(text, [a for call in fake.calls for a in call])

    def test_a_buffer_per_call(self):
        fake = Tmux()
        first = self.send(fake, "a")
        fake.calls = []
        self.assertNotEqual(self.send(fake, "a"), first)

    def test_failure(self):
        for step, commands in (
            ("load-buffer", ["load-buffer"]),
            ("paste-buffer", ["load-buffer", "paste-buffer", "delete-buffer"]),
        ):
            with self.subTest(step=step):
                fake, sleep = Tmux(fail={step}), unittest.mock.Mock()
                with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
                    tmux_host.send("s", "hi", proc=fake, sleep=sleep)
                self.assertEqual(fake.commands(), commands)
                sleep.assert_not_called()


class PasteTest(Case):
    def test_loads_the_buffer_then_pastes_it_bracketed_deleting_it(self):
        fake = Tmux()
        self.assertIsNone(tmux_host.paste("s", "a;\nb", proc=fake))
        buffer = fake.calls[0][3]
        self.assertRegex(buffer, r"^tui-s-[0-9a-f]{32}$")
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", "=s:"],
            ],
        )
        self.assertEqual(
            fake.kwargs, [{"capture_output": True, "text": True, "input": "a;\nb"}, TMUX_KW]
        )

    def test_a_failed_paste_deletes_its_buffer(self):
        for delete in ((0,), (1, "", "no buffer\n"), OSError("gone")):
            with self.subTest(delete=delete):
                fake = Tmux(results={"paste-buffer": (1, "", "boom\n"), "delete-buffer": delete})
                with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
                    tmux_host.paste("s", "hi", proc=fake)
                buffer = fake.calls[0][3]
                self.assertEqual(
                    fake.calls[1:],
                    [
                        ["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", "=s:"],
                        ["tmux", "delete-buffer", "-b", buffer],
                    ],
                )
        fake = Tmux(fail={"load-buffer"})
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            tmux_host.paste("s", "hi", proc=fake)
        self.assertEqual(fake.commands(), ["load-buffer"])


class KeysTest(Case):
    def test_send_keys_names_the_keys(self):
        fake = Tmux()
        self.assertIsNone(tmux_host.keys("s", "Escape", "C-c", "Enter", proc=fake))
        self.assertEqual(fake.calls, [["tmux", "send-keys", "-t", "=s:", "Escape", "C-c", "Enter"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_a_name_tmux_would_misread_is_refused_before_tmux(self):
        for name in ("-l", "-", "Enter;", ";"):
            with self.subTest(name=name):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, "^tmux would misread"):
                    tmux_host.keys("s", "Escape", name, proc=fake)
                self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            tmux_host.keys("s", "Enter", proc=Tmux(fail={"send-keys"}))


class ReadTest(Case):
    def test_visible_pane_or_last_lines(self):
        for lines, printed, want, extra in (
            (None, "a\n  b\n\n \n", "a\n  b", []),
            (3, "1\n2\n\n3\n4\n\n\n", "\n3\n4", ["-S", "-3"]),
        ):
            with self.subTest(lines=lines):
                fake = Tmux(results={"capture-pane": (0, printed)})
                self.assertEqual(tmux_host.read("s", lines, proc=fake), want)
                self.assertEqual(
                    fake.calls, [["tmux", "capture-pane", "-p", "-J", "-t", "=s:", *extra]]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_lines_must_be_a_positive_int(self):
        fake = Tmux()
        for lines in (0, -1, True, 1.5, "3"):
            with self.subTest(lines=lines), self.assertRaises(tmux_host.TuiError):
                tmux_host.read("s", lines, proc=fake)
        self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            tmux_host.read("s", proc=Tmux(fail={"capture-pane"}))


class ShowTest(Case):
    def show(self, fake, template=None, env=None, session="s", **kw):
        self.stderr = io.StringIO()
        with environ(**(env or {})), which(), redirect_stderr(self.stderr):
            return tmux_host.show(session, template, proc=fake, script=tmux_host.__file__, **kw)

    def test_attach_line_then_the_template(self):
        fake = Tmux()
        self.assertIsNone(self.show(fake, "open {{session}} --as {{session}}"))
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open s --as s"]])
        self.assertEqual(fake.kwargs, [SH_KW])
        self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_attach_line_takes_the_prefix(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env):
                self.show(Tmux(), "", env)
                self.assertEqual(
                    self.stderr.getvalue(), f"tui: session s: {prefix}tmux attach -t '=s'\n"
                )

    def test_session_quoted(self):
        fake = Tmux()
        self.show(fake, "open {{session}}", session="a;b c")
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open 'a;b c'"]])

    def test_failures_printed_and_returned(self):
        missing = FileNotFoundError(2, "No such file or directory", "/bin/sh")
        for template, result, why in (
            ("open {{name}} {{session}}", None, "unknown placeholder {{name}}"),
            ("false", (3,), "exit status 3"),
            ("sleep 99", TimeoutExpired("/bin/sh", 30), "timed out after 30 s"),
            ("x", missing, str(missing)),
        ):
            with self.subTest(why=why):
                fake = Tmux(results={"/bin/sh": result} if result else {})
                self.assertEqual(self.show(fake, template), why)
                self.assertEqual(self.stderr.getvalue(), ATTACH + f"tui: show: {why}\n")
                self.assertEqual(len(fake.calls), 1 if result else 0)


class GridLayoutTest(Case):
    """Window 160x48, manager %0 79 wide."""

    def tiles(self, cell):
        """Each container's children tile it exactly, one-cell borders between them."""
        if cell.kind == tmux_host.LEAF:
            return
        self.assertGreaterEqual(len(cell.children), 2)
        across = cell.kind == tmux_host.LEFT_RIGHT
        at = cell.x if across else cell.y
        for child in cell.children:
            if across:
                self.assertEqual((child.x, child.y, child.h), (at, cell.y, cell.h))
            else:
                self.assertEqual((child.y, child.x, child.w), (at, cell.x, cell.w))
            at += (child.w if across else child.h) + 1
            self.tiles(child)
        self.assertEqual(at - 1, cell.x + cell.w if across else cell.y + cell.h)

    def check(self, root, n, per_column, width=160, height=48):
        """FR-3: main cell i (pane %i+1) in column i // N, row i % N; equal widths and heights, each
        +-1.
        """
        self.tiles(root)
        self.assertEqual(
            (root.kind, root.w, root.h, root.x, root.y),
            (tmux_host.LEFT_RIGHT, width, height, 0, 0),
        )
        manager, area = root.children
        self.assertEqual((manager.pane, manager.h), ("%0", height))
        c = -(-n // per_column)
        if c > 1:
            self.assertEqual(area.kind, tmux_host.LEFT_RIGHT)
        columns = list(area.children) if c > 1 else [area]
        self.assertEqual(len(columns), c)
        self.assertLessEqual(max(col.w for col in columns) - min(col.w for col in columns), 1)
        for j, column in enumerate(columns):
            cells = list(column.children) if column.kind == tmux_host.TOP_BOTTOM else [column]
            self.assertEqual(
                [cell.pane for cell in cells],
                [f"%{i + 1}" for i in range(j * per_column, min(n, (j + 1) * per_column))],
            )
            self.assertLessEqual(max(cell.h for cell in cells) - min(cell.h for cell in cells), 1)

    def test_fr3_layouts(self):
        # the root: the manager, then the columns
        for per_column, n, want in (
            (3, 1, "160x48,0,0{79x48,0,0,0,80x48,80,0,1}"),
            (3, 2, "160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0,1,80x23,80,25,2]}"),
            (
                3,
                4,
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0[40x16,80,0,1,40x15,80,17,2,40x15,80,33,3],"
                    "39x48,121,0,4}}"
                ),
            ),
            (
                3,
                7,
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0{26x48,80,0[26x16,80,0,1,26x15,80,17,2,26x15,80,33,3],"
                    "26x48,107,0[26x16,107,0,4,26x15,107,17,5,26x15,107,33,6],26x48,134,0,7}}"
                ),
            ),
            (
                2,
                3,
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0[40x24,80,0,1,40x23,80,25,2],39x48,121,0,3}}"
                ),
            ),
        ):
            with self.subTest(per_column=per_column, n=n):
                root = grid(n, per_column)
                self.assertEqual(body(root), want)
                self.check(root, n, per_column)

    def test_fr3_any_count(self):
        for per_column in (1, 2, 3, 4):
            for n in range(1, 13):
                with self.subTest(per_column=per_column, n=n):
                    self.check(grid(n, per_column), n, per_column)

    def test_manager_width_kept(self):
        for width in (1, 30, 100):
            with self.subTest(width=width):
                root = grid(4, manager_width=width)
                self.assertEqual(root.children[0], tmux_host.Cell(width, 48, 0, 0, "%0"))
                self.check(root, 4, 3)

    def test_compress_when_too_wide(self):
        root = grid(7, manager_width=158)
        self.assertEqual(root.children[0].w, 154)
        self.assertEqual([col.w for col in root.children[1].children], [1, 1, 1])
        self.check(root, 7, 3)
        self.assertEqual(
            body(grid([slot("%1", slot("%2"))], manager_width=200)),
            "160x48,0,0{156x48,0,0,0,3x48,157,0{1x48,157,0,1,1x48,159,0,2}}",
        )
        self.assertEqual(
            grid([slot("%1", slot("%2", slot("%3")))], manager_width=200).children[0].w, 152
        )

    def test_a_manager_as_wide_as_the_window_gets_half(self):
        for width in (160, 161):
            with self.subTest(width=width):
                root = grid(2, width=width, manager_width=width)
                self.assertEqual(root, grid(2, width=width, manager_width=(width - 1) // 2))
                self.assertEqual(root.children[0].w, (width - 1) // 2)
        self.assertEqual(grid(2, manager_width=159).children[0].w, 158)

    def test_infeasible(self):
        self.assertEqual(grid(7, width=7).children[0].w, 1)
        self.assertIsNone(grid(7, width=6))
        self.assertIsNotNone(grid(3, height=5))
        self.assertIsNone(grid(3, height=4))
        self.assertIsNone(grid([slot("%1", slot("%2"), slot("%3"))], height=2))
        self.assertIsNone(grid(1, manager_width=0))

    def test_sub_cells(self):
        for case, cells, want in (
            (
                "sub-workers: the right half, equal heights",
                [slot("%1"), slot("%2", slot("%3"), slot("%4")), slot("%5")],
                "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17{40x15,80,17,2,"
                "39x15,121,17[39x7,121,17,3,39x7,121,25,4]},80x15,80,33,5]}",
            ),
            (
                "a grandchild halves the child's cell",
                [slot("%1"), slot("%2", slot("%3", slot("%6")), slot("%4")), slot("%5")],
                (
                    "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17{40x15,80,17,2,"
                    "39x15,121,17[39x7,121,17{19x7,121,17,3,19x7,141,17,6},39x7,121,25,4]},80x15,80,33,5]}"
                ),
            ),
            (
                "an orphan group stacks full width",
                [slot("%1"), slot(None, slot("%3", slot("%6")), slot("%4")), slot("%5")],
                "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17[80x7,80,17{"
                "40x7,80,17,3,39x7,121,17,6},80x7,80,25,4],80x15,80,33,5]}",
            ),
            (
                "one child: no container",
                [slot(None, slot("%3"))],
                "160x48,0,0{79x48,0,0,0,80x48,80,0,3}",
            ),
            (
                "one cell: no container",
                [slot("%1", slot("%2"))],
                "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0,1,39x48,121,0,2}}",
            ),
        ):
            with self.subTest(case):
                root = grid(cells)
                self.assertEqual(body(root), want)
                self.tiles(root)

    def test_leaves_depth_first(self):
        root = grid(
            [slot("%1"), slot("%2", slot("%3", slot("%6")), slot("%4")), slot("%5"), slot("%7")]
        )
        self.assertEqual(
            [leaf.pane for leaf in tmux_host._leaves(root)],
            ["%0", "%1", "%2", "%3", "%6", "%4", "%5", "%7"],
        )
        self.assertEqual(
            tmux_host._panes(root),
            ["%0", "%1", "%2", "%3", "%6", "%4", "%5", "%7"],
        )

    def test_panes_require_leaf_pane(self):
        with self.assertRaises(AssertionError):
            tmux_host._panes(tmux_host.Cell(1, 1, 0, 0))


class SwapsTest(Case):
    def test_every_order_of_four_correct_and_minimal(self):
        have = ["%0", "%1", "%2", "%3"]
        for want in itertools.permutations(have):
            with self.subTest(want=want):
                swaps = tmux_host._swaps(have, list(want))
                order = list(have)
                for source, target in swaps:
                    i, j = order.index(source), order.index(target)
                    order[i], order[j] = order[j], order[i]
                self.assertEqual(order, list(want))
                seen, cycles = set(), 0
                for start in have:
                    cycles += start not in seen
                    while start not in seen:
                        seen.add(start)
                        start = want[have.index(start)]
                self.assertEqual(len(swaps), len(have) - cycles)


class LayoutStringTest(Case):
    def test_checksum_of_tmux_samples(self):
        for sample in (MAIN_VERTICAL, TILED_SPLIT):
            csum, text = sample.split(",", 1)
            self.assertEqual(tmux_host._checksum(text), csum)

    def test_parse(self):
        Cell, lr, tb = tmux_host.Cell, tmux_host.LEFT_RIGHT, tmux_host.TOP_BOTTOM
        want = Cell(
            160,
            48,
            0,
            0,
            kind=lr,
            children=(
                Cell(80, 48, 0, 0, "%0"),
                Cell(
                    79,
                    48,
                    81,
                    0,
                    kind=tb,
                    children=(Cell(79, 24, 81, 0, "%1"), Cell(79, 23, 81, 25, "%2")),
                ),
            ),
        )
        self.assertEqual(tmux_host._parse_layout(MAIN_VERTICAL), want)
        self.assertEqual(tmux_host._parse_layout(MAIN_VERTICAL.split(",", 1)[1]), want)

    def test_round_trip(self):
        for sample in (MAIN_VERTICAL, TILED_SPLIT):
            self.assertEqual(tmux_host._render_layout(tmux_host._parse_layout(sample)), sample)

    def test_malformed(self):
        text = MAIN_VERTICAL.split(",", 1)[1]
        for bad in (
            "",
            "7f31",
            "7f31,",
            "0000," + text,
            "7F31," + text,
            text[:-1],
            text + "}",
            text + ",1x1,0,0,9",
            text.replace("]", "}"),
            "160x48,0,0",
            "160x48,0,0{}",
            "160x48,0,0,%0",
            "160x48,0,0[1x1,0,0,1x1,0,2,2]",
            "160x48,0,0,1\n",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(tmux_host._parse_layout(bad))


class ContainerTest(Case):
    """In a Linux container on a Mac: the caller in tmux session `cmd`, pane %0 (tty /dev/pts/0),
    shown by a client on /dev/pts/1 (iTerm2's tmux -CC through docker exec -it, control mode; else a
    plain docker exec -it tmux attach); no iTerm2 and no iTerm2 variables. pgrep finds nothing:
    procps, busybox's usage error, or none installed.
    """

    SOCK = "/tmp/tmux-0/default"

    ENV = {"TMUX": f"{SOCK},42,0", "TMUX_PANE": "%0", "PATH": "/usr/local/bin:/usr/bin:/bin"}

    OWN = ["tmux", "display-message", "-p", "-t", "%0", "#{session_name}"]

    PGREPS = (
        (1, "", ""),
        (1, "", "BusyBox v1.36.1 multi-call binary.\n\nUsage: pgrep [-flanovx] PATTERN\n"),
        (2, "", "pgrep: invalid option -- 'a'\n"),
        FileNotFoundError(2, "No such file or directory", "pgrep"),
    )

    @classmethod
    def fake(cls, pgrep, rows=(), control="1"):
        def list_panes(argv):
            if argv[-1] == "#{pane_id}":
                return 0, "%0\n%5\n"
            return expected_panes(("0", "/dev/pts/0", "%0"), ("0", "/dev/pts/2", "%5"))(argv)

        return Tmux(
            results={
                "display-message": (0, "cmd\n"),
                "list-panes": list_panes,
                "pgrep": pgrep,
                "list-sessions": (0, "".join("\t".join(r) + "\n" for r in rows)),
                "list-clients": (0, f"100 /dev/pts/1 %0 {control} {cls.SOCK}\n"),
                "split-window": (0, "%6\n"),
            }
        )

    def show(self, fake, env=ENV):
        self.stderr = io.StringIO()
        with environ(**env), which(), redirect_stderr(self.stderr):
            return tmux_host.show("s", proc=fake, script=tmux_host.__file__)

    def test_first_pane_a_tmux_split_right_of_the_callers(self):
        fake = self.fake(self.PGREPS[0])
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [
                self.OWN,
                SESSIONS,
                clients_of("cmd"),
                split_window("-h", "%0", self.SOCK),
                *record("cmd", "%6"),
            ],
        )
        self.assertEqual(self.stderr.getvalue(), ATTACH)
        for pgrep in self.PGREPS:
            with self.subTest(pgrep=pgrep):
                fake = self.fake(pgrep, control="0")
                self.assertIsNone(self.show(fake))
                self.assertEqual(
                    fake.calls,
                    [
                        self.OWN,
                        SESSIONS,
                        clients_of("cmd"),
                        HOSTS,
                        PGREP,
                        split_window("-h", "%0", self.SOCK),
                        *record("cmd", "%6"),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_a_second_pane_stacks_below_the_first(self):
        fake = self.fake(self.PGREPS[0], [("$1", "w1", "cmd", "%5", self.SOCK)])
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [self.OWN, SESSIONS, LIVE, split_window("-v", "%5", self.SOCK), *record("cmd", "%6")],
        )
        self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_no_client_no_pane_and_the_prefixed_attach_command(self):
        fake = self.fake(self.PGREPS[0])
        fake.results["list-clients"] = (0, "")
        why = self.show(fake, {**self.ENV, "TUI_ATTACH_PREFIX": "docker exec -it box"})
        self.assertTrue(why.startswith("no anchor pane: no terminal shows tmux session cmd"), why)
        self.assertTrue(
            why.endswith("; watch it with docker exec -it box tmux attach -t '=s'"), why
        )
        self.assertNotIn("split-window", fake.commands())
        self.assertNotIn("osascript", fake.commands())


class ListingTest(Case):
    def listing(self, sessions, clients=(0, ""), panes=(0, ""), capture=(0, ""), match=None):
        fake = Tmux(
            results={
                "list-sessions": sessions,
                "list-clients": clients,
                "list-panes": panes,
                "capture-pane": capture,
            }
        )
        return tmux_host.listing(match, proc=fake), fake

    def test_every_field_sorted_by_name_one_call_each(self):
        got, fake = self.listing(
            listed(
                ("w2", "0", "sid-2", "fake", "done", "%7", "mgr"),
                ("w1", "1", "sid-1", "other", "dead", "ABC-1", "w0t0p0:ABC-1"),
                ("mgr", "0"),
            ),
            tabbed(
                ("200", "/dev/ttys4", "w2"), ("100", "/dev/ttys9", "w2"), ("5", "/dev/ttys1", "mgr")
            ),
            tabbed(("0", "/dev/ttys4", "mgr"), ("0", "/dev/ttys2", "w2")),
            (0, "a\nlast\n\n"),
        )
        self.assertEqual(
            got,
            [
                expected_row("mgr", shown=""),
                expected_row(
                    "w1",
                    sid="sid-1",
                    runtime="other",
                    state="dead",
                    pane="ABC-1",
                    opener="w0t0p0:ABC-1",
                    dead=True,
                    last_lines="a\nlast",
                ),
                expected_row(
                    "w2",
                    sid="sid-2",
                    runtime="fake",
                    state="done",
                    pane="%7",
                    opener="mgr",
                    shown="mgr",
                ),
            ],
        )
        self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS, HOST_PANES, captured("w1")])
        self.assertEqual(fake.kwargs, [TMUX_KW] * 4)
        self.assertEqual(tmux_host.LAST_LINES, 2000)

    def test_a_name_failing_NAME_or_a_row_tmux_split_wrongly_is_dropped(self):
        got, fake = self.listing(
            (0, "".join(f"{n}\t0\t\t\t\t\t\t\n" for n in ("a b", "", "%1", "x;", "#{x}", "ok")))
        )
        self.assertEqual(got, [expected_row("ok")])
        got, _ = self.listing((0, "w1\t0\ts\tid\t\t\t\t\t\nw2\t0\nw3\t0\t\t\t\t\t\t\n"))
        self.assertEqual(got, [expected_row("w3")])
        forged = listed(("w4", "0", "", "", "", "", "", "mgr\tx"), ("w5", "0"))
        got, _ = self.listing(forged)
        self.assertEqual(got, [expected_row("w5")])
        got, fake = self.listing(listed(("a b", "0")))
        self.assertEqual((got, fake.calls), ([], [LISTING]))

    def test_a_bad_or_empty_field_is_none(self):
        for key, value, want in (
            ("sid", "sid-1", "sid-1"),
            ("sid", "", None),
            ("sid", "s\x1b]0;x\x07", None),
            ("sid", "s\x7f", None),
            ("runtime", "fake", "fake"),
            ("runtime", "", None),
            ("runtime", "\x1b[2J", None),
            ("state", "done", "done"),
            ("state", "", None),
            ("state", "do\x08ne", None),
            ("pane", "%3", "%3"),
            ("pane", "ABC-12", "ABC-12"),
            ("pane", "", None),
            ("pane", "%", None),
            ("pane", "%3;", None),
            ("pane", "w0t0p0:ABC", None),
            ("opener", "mgr", "mgr"),
            ("opener", "w0t0p0:ABC-1", "w0t0p0:ABC-1"),
            ("opener", "", None),
            ("opener", "a b", None),
            ("opener", "x:", None),
            ("opener", "%3", None),
            ("display", "mgr/right/m1", "mgr/right/m1"),
            ("display", "elsewhere:1.2", "elsewhere:1.2"),
            ("display", "", None),
            ("display", "mgr;", None),
            ("display", "a\x1bb", None),
            ("dead", "0", False),
            ("dead", "1", True),
            ("dead", "", None),
            ("dead", "2", None),
        ):
            with self.subTest(key=key, value=value):
                fields = {**dict.fromkeys(COLUMNS, ""), "name": "s", "dead": "0", key: value}
                got, _ = self.listing(listed(tuple(fields[c] for c in COLUMNS)))
                self.assertEqual(got[0][key], want)

    def test_a_dead_rows_last_lines_live_rows_none(self):
        printed = {
            "=d1:": (0, "x\n  y\n\n"),
            "=d2:": (1, "", "boom\n"),
            "=d3:": (0, "\n \n"),
            "=d4:": FileNotFoundError(2, "No such file", "tmux"),
            "=d5:": (0, "".join(f"{i}\n" for i in range(2001)) + "\n\n"),
        }
        got, fake = self.listing(
            listed(*((n, "1") for n in ("d1", "d2", "d3", "d4", "d5")), ("live", "0"), ("odd", "")),
            capture=lambda argv: printed[argv[argv.index("-t") + 1]],
        )
        self.assertEqual(
            [(r["name"], r["last_lines"]) for r in got],
            [
                ("d1", "x\n  y"),
                ("d2", None),
                ("d3", None),
                ("d4", None),
                ("d5", "\n".join(map(str, range(1, 2001)))),
                ("live", None),
                ("odd", None),
            ],
        )
        self.assertEqual(fake.calls[2:], [captured(n) for n in ("d1", "d2", "d3", "d4", "d5")])

    def test_shown_by_the_most_recent_client_in_its_host_session(self):
        names = ("a", "b", "c", "d", "e", "f", "g", "h", "i")
        got, fake = self.listing(
            listed(*((n, "0") for n in names)),
            tabbed(
                ("7", "/dev/host", "b"),
                ("7", "/dev/term", "c"),
                ("7", "/dev/bad", "d"),
                ("7", "/dev/gone", "e"),
                ("10", "/dev/host", "f"),
                ("20", "/dev/term", "f"),
                ("9", "/dev/term", "g"),
                ("10", "/dev/host", "g"),
                ("x", "/dev/host", "h"),
                ("7", "", "h"),
                ("7", "/dev/host", "h", "extra"),
                ("7", "/dev/host"),
                ("7", "/dev/host", "zz"),
            ),
            tabbed(
                ("0", "/dev/host", "host"),
                ("0", "/dev/bad", "bad name"),
                ("1", "/dev/gone", "host2"),
            ),
        )
        self.assertEqual(
            {r["name"]: r["shown"] for r in got},
            {
                "a": None,
                "b": "host",
                "c": "",
                "d": "",
                "e": "",
                "f": "",
                "g": "host",
                "h": None,
                "i": None,
            },
        )
        self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS, HOST_PANES])

    def test_host_panes_only_when_a_listed_session_has_a_client(self):
        for clients, match, want in (
            ((0, ""), None, [expected_row("mgr"), expected_row("w1")]),
            (tabbed(("7", "/dev/host", "w1")), re.compile("mgr"), [expected_row("mgr")]),
        ):
            with self.subTest(clients=clients):
                got, fake = self.listing(listed(("mgr", "0"), ("w1", "0")), clients, match=match)
                self.assertEqual(got, want)
                self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS])

    def test_match_fullmatches_before_any_capture(self):
        sessions = listed(("w1", "1"), ("w10", "1"), ("mgr", "1"), ("xw1", "0"))
        for match, want in (
            (re.compile("w1"), ["w1"]),
            (re.compile("w[0-9]+"), ["w1", "w10"]),
            (None, ["mgr", "w1", "w10", "xw1"]),
        ):
            with self.subTest(match=match):
                got, fake = self.listing(sessions, match=match)
                self.assertEqual([r["name"] for r in got], want)
                self.assertEqual(
                    fake.calls, [LISTING, ALL_CLIENTS, *(captured(n) for n in want if n != "xw1")]
                )
        got, fake = self.listing(sessions, match=re.compile("w"))
        self.assertEqual((got, fake.calls), ([], [LISTING]))

    def test_no_server_is_empty(self):
        for result in (*((1, "", err + "\n") for err in ABSENT), (0, "")):
            with self.subTest(result=result):
                got, fake = self.listing(result)
                self.assertEqual((got, fake.calls), ([], [LISTING]))

    def test_tmux_failing_is_none(self):
        sessions, client = listed(("w1", "0")), tabbed(("7", "/dev/host", "w1"))
        for failing in (
            *((1, "", err + "\n") for err in FAILED),
            FileNotFoundError(2, "No such file", "tmux"),
        ):
            for at, calls in (
                ({"sessions": failing}, [LISTING]),
                ({"clients": failing}, [LISTING, ALL_CLIENTS]),
                ({"clients": client, "panes": failing}, [LISTING, ALL_CLIENTS, HOST_PANES]),
            ):
                with self.subTest(failing=failing, at=at):
                    got, fake = self.listing(**{"sessions": sessions, **at})
                    self.assertEqual((got, fake.calls), (None, calls))


class OptionTest(Case):
    def test_value(self):
        for printed, want in (("0123abcd\n", "0123abcd"), ("a b\n", "a b"), ("\n", "")):
            with self.subTest(printed=printed):
                fake = Tmux(results={"show-options": (0, printed)})
                self.assertEqual(tmux_host.option("s", "@sid", proc=fake), want)
                self.assertEqual(fake.calls, [["tmux", "show-options", "-v", "-t", "=s:", "@sid"]])
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_none_when_unset_or_absent(self):
        for err in ("invalid option: @sid", *ABSENT):
            with self.subTest(err=err):
                fake = Tmux(results={"show-options": (1, "", err + "\n")})
                self.assertIsNone(tmux_host.option("s", "@sid", proc=fake))

    def test_another_failure_raises(self):
        for result in (
            (1, "", FAILED[0] + "\n"),
            (1, "", "boom\n"),
            FileNotFoundError(2, "No such file", "tmux"),
        ):
            with self.subTest(result=result), self.assertRaisesRegex(tmux_host.TuiError, "^tmux: "):
                tmux_host.option("s", "@sid", proc=Tmux(results={"show-options": result}))

    def test_only_a_user_option(self):
        for name in ("status", "@", "@a b", "@a;", "", "-g"):
            with self.subTest(name=name):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, "^option "):
                    tmux_host.option("s", name, proc=fake)
                self.assertEqual(fake.calls, [])


class PaneIdTest(Case):
    def test_its_pane(self):
        fake = Tmux(pane="%5")
        self.assertEqual(tmux_host.pane("s", proc=fake), "%5")
        self.assertEqual(fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", "#{pane_id}"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_none_when_absent(self):
        # tmux (3.7) exits 0 printing nothing for a missing session
        for result in (*((1, "", err + "\n") for err in ABSENT), (0, "\n"), (0, "")):
            with self.subTest(result=result):
                self.assertIsNone(
                    tmux_host.pane("s", proc=Tmux(results={"display-message": result}))
                )

    def test_a_failure_or_junk_raises(self):
        for result, error in (
            ((1, "", "boom\n"), "^tmux: boom$"),
            ((1, "", FAILED[0] + "\n"), "^tmux: "),
            ((0, "%5;\n"), "^bad tmux pane '%5;'$"),
            ((0, "5\n"), "^bad tmux pane '5'$"),
            ((0, "%5\n%6\n"), "^bad tmux pane"),
        ):
            with self.subTest(result=result), self.assertRaisesRegex(tmux_host.TuiError, error):
                tmux_host.pane("s", proc=Tmux(results={"display-message": result}))


class PaneTargetTest(Case):
    """status, kill, paste, keys, option and read take a pane id (%N) as their target."""

    def calls(self, target, fake):
        return [
            lambda: tmux_host.status(target, proc=fake),
            lambda: tmux_host.kill(target, proc=fake),
            lambda: tmux_host.paste(target, "hi", proc=fake),
            lambda: tmux_host.keys(target, "Enter", proc=fake),
            lambda: tmux_host.option(target, "@sid", proc=fake),
            lambda: tmux_host.read(target, proc=fake),
        ]

    def test_a_pane_id_is_the_target_as_is(self):
        fake = Tmux(pane="0  ")
        for call in self.calls("%12", fake):
            call()
        buffer = fake.calls[2][3]
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "display-message", "-p", "-t", "%12", FORMAT],
                ["tmux", "kill-session", "-t", "%12"],
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", "%12"],
                ["tmux", "send-keys", "-t", "%12", "Enter"],
                ["tmux", "show-options", "-v", "-t", "%12", "@sid"],
                ["tmux", "capture-pane", "-p", "-J", "-t", "%12"],
            ],
        )

    def test_a_gone_pane_is_absent(self):
        gone = (1, "", "can't find pane: %12\n")
        self.assertIsNone(tmux_host.status("%12", proc=Tmux(results={"display-message": gone})))
        self.assertIsNone(
            tmux_host.option("%12", "@sid", proc=Tmux(results={"show-options": gone}))
        )
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: can't find pane: %12$"):
            tmux_host.kill("%12", proc=Tmux(results={"kill-session": gone}))

    def test_a_bad_target_is_refused_before_tmux(self):
        for target in ("%", "%x", "%1;", "%1 ", "%1:", "%1.0", "=%1", "%-1", "a b"):
            with self.subTest(target=target):
                fake = Tmux()
                for call in self.calls(target, fake):
                    with self.assertRaisesRegex(tmux_host.TuiError, "^invalid target "):
                        call()
                self.assertEqual(fake.calls, [])


class PanePlacementTest(PaneFixture):
    def test_iterm2_pane_split_from(self):
        fake = Tmux(
            results={
                "display-message": (0, "own\n"),
                "list-clients": self.clients((5, "/dev/ttys004", "%9")),
                "osascript": (0, "ok NEW\n"),
            }
        )
        self.assertIsNone(self.show(fake, INSIDE, split_from="b", split="below"))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("b"),
                HOSTS,
                PGREP,
                osa("below", "tty", "s", "/dev/ttys004"),
                *record("own", "NEW"),
            ],
        )

    def test_tmux_split_split_from(self):
        clients = self.clients((5, "/dev/ttys004", "%9"), (9, "/dev/ttys005", "%7"))
        fake = Tmux(results={"list-clients": clients, "pgrep": (1,), "split-window": (0, "%10\n")})
        self.assertIsNone(self.show(fake, {}, split_from="b", split="below"))
        self.assertEqual(fake.calls[-2:], [split_window("-v", "%7"), *record(None, "%10")])
        nested = Tmux(
            results={
                "list-clients": clients,
                "list-panes": expected_panes(("0", "/dev/ttys005", "%5")),
                "split-window": (0, "%10\n"),
            }
        )
        self.assertIsNone(self.show(nested, {}, split_from="b"))
        self.assertEqual(
            nested.calls, [clients_of("b"), HOSTS, split_window("-h", "%5"), *record(None, "%10")]
        )

    def test_control_mode_client_gets_a_tmux_split_of_own_pane(self):
        # iTerm2's tmux -CC: the control-mode client's tty is the hidden gateway tab's; iTerm2 draws
        # the tmux split
        clients = self.clients((300, "/dev/ttys009", "%1", "1"), (100, "/dev/ttys001", "%2"))
        for split, flag in ((None, "-h"), ("right", "-h"), ("below", "-v")):
            with self.subTest(split=split):
                fake = Tmux(
                    results={
                        "display-message": (0, "own\n"),
                        "list-clients": clients,
                        "split-window": (0, "%10\n"),
                    }
                )
                self.assertIsNone(self.show(fake, INSIDE, split=split))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of("own"),
                        split_window(flag, "%3"),
                        *record("own", "%10"),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_split_from_a_session_only_a_control_mode_client_shows(self):
        clients = self.clients((5, "/dev/ttys009", "%9", "1"))
        for env, head, opener in (({}, [], None), (INSIDE, [OWN, SESSIONS], "own")):
            with self.subTest(env=env):
                fake = Tmux(
                    results={
                        "display-message": (0, "own\n"),
                        "list-clients": clients,
                        "split-window": (0, "%10\n"),
                    }
                )
                self.assertIsNone(self.show(fake, env, split_from="b", split="below"))
                self.assertEqual(
                    fake.calls,
                    [*head, clients_of("b"), split_window("-v", "%9"), *record(opener, "%10")],
                )

    def test_control_mode_ttys_are_never_iterm2_anchors(self):
        fake = Tmux(
            results={
                "display-message": (0, "own\n"),
                "osascript": (0, "ok NEW\n"),
                "list-clients": self.clients(
                    (300, "/dev/ttys003", "%2"),
                    (200, "/dev/ttys009", "%1", "1"),
                    (100, "/dev/ttys001", "%1"),
                ),
            }
        )
        self.assertIsNone(self.show(fake, INSIDE))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("own"),
                HOSTS,
                PGREP,
                osa("right", "tty", "s", "/dev/ttys003", "/dev/ttys001"),
                *record("own", "NEW"),
            ],
        )

    def test_tmux_split_failures(self):
        clients = self.clients((5, "/dev/ttys004", "%9"))
        fake = Tmux(
            results={
                "list-clients": clients,
                "pgrep": (1,),
                "split-window": (1, "", "no space for new pane\n"),
            }
        )
        self.assertEqual(self.show(fake, {}, split_from="b"), "tmux: no space for new pane")
        self.assertNotIn("set-option", fake.commands())
        fake = Tmux(results={"list-clients": (0, "5 /dev/ttys004 %9 0 /tmp/#s\n"), "pgrep": (1,)})
        self.assertEqual(
            self.show(fake, {}, split_from="b"), "tmux would misread '/tmp/#s'" + WATCH
        )
        self.assertNotIn("split-window", fake.commands())

    def test_split_from_not_shown_anywhere(self):
        for result in ((0, ""), (1, "", "can't find session: =b\n")):
            with self.subTest(result=result):
                fake = Tmux(results={"list-clients": result})
                self.assertTrue(self.show(fake, {}, split_from="b").endswith(WATCH))
                self.assertEqual(fake.commands(), ["list-clients"])

    def test_bad_arguments_raise(self):
        for kw in (
            {"split_from": "a b"},
            {"split_from": "=b"},
            {"split": "up"},
            {"split": "vertically"},
            {"split": ""},
            {"opener": "a b"},
            {"opener": "a:b:c"},
        ):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaises(tmux_host.TuiError):
                    self.show(fake, ITERM, **kw)
                self.assertEqual(fake.calls, [])

    def test_template_ignores_split_split_from_and_opener(self):
        fake = Tmux()
        self.assertIsNone(
            self.show(
                fake,
                INSIDE,
                template="open {{session}}",
                split="up",
                split_from="a b",
                opener="a b",
            )
        )
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open s"]])

    def test_no_anchor_refuses_with_the_attach_command(self):
        for env in (
            {},
            {"ITERM_SESSION_ID": "w0t0p0"},
            {"ITERM_SESSION_ID": ""},
            {"ITERM_SESSION_ID": "w0t0p0:ABC"},
            {**ITERM, "TERM_PROGRAM": "Apple_Terminal"},
        ):
            for split in (None, "right"):
                with self.subTest(env=env, split=split):
                    fake = Tmux()
                    why = self.show(fake, env, split=split)
                    self.assertRegex(
                        why,
                        r"^no anchor pane: not in tmux, no iTerm2 pane \(\$ITERM_SESSION_ID\)"
                        + WATCH
                        + "$",
                    )
                    self.assertEqual(fake.calls, [])
                    self.assertEqual(self.stderr.getvalue(), ATTACH + f"tui: show: {why}\n")

    def test_the_watch_command_takes_the_prefix(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env):
                why = self.show(Tmux(), env)
                self.assertTrue(why.endswith(f"; watch it with {prefix}tmux attach -t '=s'"), why)

    def test_no_tmux(self):
        fake = Tmux()
        self.assertEqual(self.show(fake, ITERM, tmux=None), "tmux not found")
        self.assertEqual(fake.calls, [])

    def test_tmux_path_made_absolute(self):
        here = os.getcwd()
        os.chdir(tempfile.gettempdir())
        self.addCleanup(os.chdir, here)
        fake = Tmux(results={"osascript": (0, "ok NEW\n")})
        self.show(fake, ITERM, tmux="bin/tmux")
        self.assertEqual(fake.calls[2][5], os.path.join(os.getcwd(), "bin/tmux"))

    def test_iterm_session_id_failures_refuse(self):
        missing = FileNotFoundError(2, "No such file or directory", "osascript")
        for results, why in (
            ({"pgrep": (1,)}, "iTerm2 is not running"),
            ({"osascript": missing}, f"osascript: {missing}"),
            (
                {"osascript": TimeoutExpired("osascript", 30)},
                "osascript timed out after 30 s",
            ),
            (
                {"osascript": (1, "", "execution error: boom (-1708)\n")},
                "osascript: execution error: boom (-1708)",
            ),
            (
                {"osascript": (0, "no iTerm2 pane shows the anchor\n")},
                "no iTerm2 pane shows the anchor",
            ),
        ):
            with self.subTest(why=why):
                fake = Tmux(results=results)
                self.assertEqual(self.show(fake, ITERM), why + WATCH)
                self.assertEqual(self.stderr.getvalue(), ATTACH + f"tui: show: {why}{WATCH}\n")
                self.assertNotIn("split-window", fake.commands())
                self.assertNotIn("set-option", fake.commands())

    def test_script_is_fixed_and_takes_its_values_as_arguments(self):
        script = tmux_host.APPLESCRIPT
        self.assertNotIn(TMUX, script)
        for part in (
            "on run argv",
            'if application "iTerm2" is running',
            'tell application "iTerm2"',
            "quoted form of tmuxPath",
            'quoted form of ("=" & sessionName)',
            "split vertically with default profile command",
            "split horizontally with default profile command",
            "unique id of s",
            "tty of s",
            'return "ok " & (unique id of newSession)',
        ):
            self.assertIn(part, script)
        # its panes end with their tmux session
        self.assertEqual(script.count("with default profile command paneCommand"), 2)
        for word in ("write text", "close"):
            self.assertNotIn(word, script)


class StackTest(Case):
    """Panes stack by opener. The caller is in tmux session `own`, pane %3, shown by a terminal
    client (tty /dev/ttys004) whose pane is %9; list-sessions prints `rows`, list-panes -a the
    `live` pane ids (`hosts`, panes() rows, for the tty format); pgrep finds no iTerm2 unless told;
    a tmux split prints %99.
    """

    NONE = (0, "no iTerm2 pane shows the anchor\n")

    def fake(
        self,
        rows=(),
        live=(),
        own="cmd",
        clients=f"5 /dev/ttys004 %9 0 {SOCK}\n",
        hosts=(),
        **results,
    ):
        def list_panes(argv):
            if "-a" not in argv:  # a grid read: a window with no grid
                return 0, f"@1\t\t{argv[3]}\t80\t24\t{SOCK}\n"
            return (
                (0, "".join(p + "\n" for p in live))
                if argv[-1] == "#{pane_id}"
                else expected_panes(*hosts)(argv)
            )

        return Tmux(
            results={
                "display-message": (0, own + "\n"),
                "list-sessions": (0, "".join("\t".join(r) + "\n" for r in rows)),
                "list-panes": list_panes,
                "list-clients": (0, clients),
                "pgrep": (1,),
                "split-window": (0, "%99\n"),
                **results,
            }
        )

    def show(self, fake, env=INSIDE, **kw):
        self.stderr = io.StringIO()
        with environ(**env), which(), redirect_stderr(self.stderr):
            return tmux_host.show(
                "s", kw.pop("template", None), **kw, proc=fake, script=tmux_host.__file__
            )

    def expected_row(self, number, name, pane, opener="cmd"):
        return f"${number}", name, opener, pane, SOCK

    def test_first_pane_right_of_the_opener(self):
        for results in ({}, {"list-sessions": (1, "", "no server running\n")}):
            with self.subTest(results=results):
                fake = self.fake(**results)
                self.assertIsNone(self.show(fake))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of("cmd"),
                        HOSTS,
                        PGREP,
                        split_window("-h", "%3"),
                        *record("cmd", "%99"),
                    ],
                )
                self.assertEqual(self.stderr.getvalue(), ATTACH)
        fake = self.fake(pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.show(fake, ITERM))
        self.assertEqual(
            fake.calls,
            [SESSIONS, PGREP, osa("right", "id", "s", "ABC"), *record("w0t0p0:ABC", "NEW")],
        )

    def test_below_the_newest_live_pane_else_right(self):
        # the second below the first; the newest closed: below the previous live one; none live:
        # right
        three = [
            self.expected_row(9, "a", "%9"),
            self.expected_row(11, "c", "%11"),
            self.expected_row(10, "b", "%10"),
        ]
        right = [clients_of("cmd"), HOSTS, PGREP, split_window("-h", "%3")]
        for rows, live, then in (
            ([self.expected_row(1, "a", "%7")], ["%3", "%7", "%9"], [split_window("-v", "%7")]),
            (three, ["%9", "%10", "%11"], [split_window("-v", "%11")]),
            (three, ["%9", "%10"], [split_window("-v", "%10")]),
            (three, ["%9"], [split_window("-v", "%9")]),
            ([self.expected_row(1, "a", "%7")], ["%3", "%9"], right),
        ):
            with self.subTest(rows=rows, live=live):
                fake = self.fake(rows, live)
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.calls, [OWN, SESSIONS, LIVE, *then, *record("cmd", "%99")])

    def test_tmux_liveness_is_an_exact_line(self):
        fake = self.fake(
            [self.expected_row(1, "a", "%1")], live=["%10", " %1", "%1 ", "x%1", "%1\r"]
        )
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls[2:],
            [
                LIVE,
                clients_of("cmd"),
                HOSTS,
                PGREP,
                split_window("-h", "%3"),
                *record("cmd", "%99"),
            ],
        )

    def test_a_worker_gets_its_own_column(self):
        stack = [
            self.expected_row(1, "a", "%7"),
            self.expected_row(2, "W", "%8"),
        ]  # the commander's (cmd); W's pane is the newest
        live = ["%3", "%7", "%8"]
        fake = self.fake(stack, live, own="W", pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                grid_read("%8"),
                clients_of("W"),
                HOSTS,
                PGREP,
                osa("right", "tty", "s", "/dev/ttys004"),
                *record("W", "NEW"),
            ],
        )
        # W shown by a nested client in the commander's pane %8: that pane splits, never one in W's
        # window
        fake = self.fake(
            stack, live, own="W", hosts=[("0", "/dev/ttys001", "%7"), ("0", "/dev/ttys004", "%8")]
        )
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                grid_read("%8"),
                clients_of("W"),
                HOSTS,
                split_window("-h", "%8"),
                *record("W", "%99"),
            ],
        )
        fake = self.fake([*stack, self.expected_row(3, "w2", "%20", "W")], [*live, "%20"], own="W")
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [OWN, SESSIONS, grid_read("%8"), LIVE, split_window("-v", "%20"), *record("W", "%99")],
        )

    def test_explicit_split_or_split_from_overrides_the_stack(self):
        for kw, (session, flag, pane) in (
            ({"split": "right"}, ("cmd", "-h", "%3")),
            ({"split": "below"}, ("cmd", "-v", "%3")),
            ({"split_from": "b"}, ("b", "-h", "%9")),
            ({"split_from": "b", "split": "below"}, ("b", "-v", "%9")),
        ):
            with self.subTest(**kw):
                fake = self.fake([self.expected_row(1, "a", "%7")], live=["%3", "%7"])
                self.assertIsNone(self.show(fake, **kw))
                self.assertEqual(
                    fake.calls,
                    [
                        OWN,
                        SESSIONS,
                        clients_of(session),
                        HOSTS,
                        PGREP,
                        split_window(flag, pane),
                        *record("cmd", "%99"),
                    ],
                )

    def test_iterm2_panes_stack_too(self):
        opener = ITERM["ITERM_SESSION_ID"]
        rows = [
            self.expected_row(4, "d", "ID4", opener),
            self.expected_row(3, "c", "%5", opener),
            self.expected_row(2, "b", "ID2", opener),
            self.expected_row(1, "a", "ID1", opener),
        ]
        tried = [
            [PGREP, osa("below", "id", "s", "ID4")],
            [LIVE, PGREP, osa("below", "id", "s", "ID2", "ID1")],
            [PGREP, osa("right", "id", "s", "ABC")],
        ]
        for n in (1, 2, 3):
            with self.subTest(osascript_calls=n):
                fake = self.fake(
                    rows, pgrep=(0,), osascript=seq(*[self.NONE] * (n - 1), (0, "ok NEW\n"))
                )
                self.assertIsNone(self.show(fake, ITERM))
                self.assertEqual(
                    fake.calls,
                    [SESSIONS, *(c for t in tried[:n] for c in t), *record(opener, "NEW")],
                )
        fake = self.fake(rows, live=["%5"], pgrep=(0,), osascript=self.NONE)
        self.assertIsNone(self.show(fake, ITERM))
        self.assertEqual(
            fake.calls,
            [SESSIONS, *tried[0], LIVE, split_window("-v", "%5"), *record(opener, "%99")],
        )
        fake = self.fake(rows)
        self.assertEqual(self.show(fake, ITERM), "iTerm2 is not running" + WATCH)
        self.assertEqual(fake.calls, [SESSIONS, PGREP, LIVE, PGREP, PGREP])

    def test_applescript_output(self):
        for out, pane in (("ok NEW\n", "NEW"), ("ok 6D7E8F90-1A2B\n", "6D7E8F90-1A2B")):
            with self.subTest(out=out):
                fake = self.fake(pgrep=(0,), osascript=(0, out))
                self.assertIsNone(self.show(fake, ITERM))
                self.assertEqual(fake.calls[-2:], record("w0t0p0:ABC", pane))
        for out, why in (
            ("ok\n", "ok"),
            ("ok \n", "ok"),
            ("ok a b\n", "ok a b"),
            ("ok a;b\n", "ok a;b"),
            ("ok a_b\n", "ok a_b"),
            ("OK ABC\n", "OK ABC"),
            ("ok A\nok B\n", "ok A\nok B"),
            ("", "osascript printed nothing"),
        ):
            with self.subTest(out=out):
                fake = self.fake(pgrep=(0,), osascript=(0, out))
                self.assertEqual(self.show(fake, ITERM), why + WATCH)
                self.assertNotIn("set-option", fake.commands())

    def test_options_only_after_a_split(self):
        for template in ("open {{session}}", ""):
            with self.subTest(template=template):
                fake = self.fake()
                self.assertIsNone(self.show(fake, template=template))
                self.assertEqual(fake.calls, [["/bin/sh", "-c", "open s"]] if template else [])
        fake = self.fake(**{"split-window": (1, "", "no space for new pane\n")})
        self.assertEqual(self.show(fake), "tmux: no space for new pane")
        self.assertNotIn("set-option", fake.commands())

    def test_a_failure_to_record_is_printed(self):
        for fail, calls in (("@opener", 1), ("@pane", 2)):
            with self.subTest(fail=fail):
                fake = self.fake(
                    **{
                        "set-option": lambda argv, *, fail=fail: (
                            (1, "", "boom\n") if fail in argv else (0,)
                        )
                    }
                )
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.commands().count("set-option"), calls)
                self.assertEqual(
                    self.stderr.getvalue(), ATTACH + f"tui: show: {fail}: tmux: boom\n"
                )
        fake = self.fake(**{"split-window": (0, "\n")})
        self.assertIsNone(self.show(fake))
        self.assertNotIn("set-option", fake.commands())
        self.assertEqual(self.stderr.getvalue(), ATTACH + "tui: show: @pane: not a pane id: ''\n")

    def test_read_back_values_must_fullmatch(self):
        bad = [
            ("$9", "a", "cmd", "%5;", SOCK),
            ("$8", "b", "cmd", "#{x}", SOCK),
            ("$7", "c", "cmd", "I D", SOCK),
            ("$7", "d", "cmd", "ID;x", SOCK),
            ("$6", "e", "cmd ", "%6", SOCK),
            ("$6", "f", "other", "%6", SOCK),
            ("$6", "g", "c:md:x", "%6", SOCK),
            ("$5", "s", "cmd", "%5", SOCK),
            ("5", "h", "cmd", "%5", SOCK),
            ("$5x", "i", "cmd", "%5", SOCK),
            ("$4", "j", "cmd", "", SOCK),
            ("$4", "k", "cmd", "%4"),
            ("$4", "l", "cmd", "%4", SOCK, "x"),
        ]
        live = ["%2", "%4", "%5", "%6"]
        fake = self.fake(bad, live)
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls,
            [
                OWN,
                SESSIONS,
                clients_of("cmd"),
                HOSTS,
                PGREP,
                split_window("-h", "%3"),
                *record("cmd", "%99"),
            ],
        )
        fake = self.fake([*bad, self.expected_row(1, "m", "%2")], live)
        self.assertIsNone(self.show(fake))
        self.assertEqual(
            fake.calls, [OWN, SESSIONS, LIVE, split_window("-v", "%2"), *record("cmd", "%99")]
        )

    def test_a_passed_opener(self):
        fake = self.fake(pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.show(fake, opener="w0t1p0:XYZ"))
        self.assertEqual(
            fake.calls,
            [SESSIONS, PGREP, osa("right", "id", "s", "XYZ"), *record("w0t1p0:XYZ", "NEW")],
        )
        fake = self.fake(own="drv")
        self.assertIsNone(self.show(fake, opener="outer"))
        self.assertEqual(
            fake.calls,
            [
                SESSIONS,
                OWN,
                clients_of("outer"),
                HOSTS,
                PGREP,
                split_window("-h", "%9"),
                *record("outer", "%99"),
            ],
        )
        fake = self.fake()
        self.assertIsNone(self.show(fake, ITERM, opener="outer"))
        self.assertEqual(
            fake.calls,
            [
                SESSIONS,
                clients_of("outer"),
                HOSTS,
                PGREP,
                split_window("-h", "%9"),
                *record("outer", "%99"),
            ],
        )
        fake = self.fake(own="outer")
        self.assertIsNone(self.show(fake, opener="outer"))
        self.assertEqual(fake.calls[-3:], [split_window("-h", "%3"), *record("outer", "%99")])
        fake = self.fake(own="drv", clients="", pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.show(fake, opener="outer"))
        self.assertEqual(
            fake.calls,
            [
                SESSIONS,
                OWN,
                clients_of("outer"),
                PGREP,
                osa("right", "id", "s", "ABC"),
                *record("outer", "NEW"),
            ],
        )
        fake = self.fake(
            [self.expected_row(2, "b", "%8"), self.expected_row(1, "a", "%7", "outer")],
            ["%7", "%8"],
            own="drv",
        )
        self.assertIsNone(self.show(fake, opener="outer"))
        self.assertEqual(
            fake.calls, [SESSIONS, LIVE, split_window("-v", "%7"), *record("outer", "%99")]
        )
        no_iterm = {k: v for k, v in INSIDE.items() if k != "ITERM_SESSION_ID"}
        fake = self.fake(own="drv", clients="")
        self.assertEqual(
            self.show(fake, no_iterm, opener="outer"),
            "no anchor pane: no terminal shows tmux session "
            "outer, no iTerm2 pane ($ITERM_SESSION_ID)" + WATCH,
        )

    def test_a_bad_opener_raises(self):
        for opener in (
            "",
            "a b",
            "w0t0p0:",
            ":ABC",
            "a:b:c",
            "a;b",
            "w0t0p0:A_B",
            "=a",
            "%3",
            "a\n",
            "a:b\n",
        ):
            with self.subTest(opener=opener):
                fake = self.fake()
                with self.assertRaisesRegex(tmux_host.TuiError, "opener"):
                    self.show(fake, opener=opener)
                self.assertEqual(fake.calls, [])


class StartTest(StartFixture):
    def test_start_runs_wrapper_decorates_dead_pane_without_opening_a_pane(
        self,
    ):
        # in tmux or iTerm2 all the same: start never shows the session
        for env in ({}, INSIDE, ITERM):
            with self.subTest(env=env), environ(**env), which():
                fake = Tmux()
                self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
                self.assertEqual(
                    fake.calls[1],
                    [
                        "tmux",
                        "new-session",
                        "-d",
                        "-s",
                        "s",
                        "-x",
                        "120",
                        "-y",
                        "40",
                        sys.executable,
                        "-I",
                        "-c",
                        tmux_host.EXEC,
                        fake.path,
                        fake.fifo,
                        ";",
                        "set-option",
                        "-p",
                        "-t",
                        "=s:",
                        "remain-on-exit",
                        "on",
                        *chained(decorations("s")),
                    ],
                )
                self.assertNotIn("-c", fake.calls[1][: fake.calls[1].index(sys.executable)])
                self.assertEqual({a for a in fake.calls[1] if a.endswith(";")}, {";"})
                self.assertEqual(fake.kwargs, [TMUX_KW] * len(fake.calls))
                self.sleep.assert_not_called()
                self.assertEqual(self.stderr.getvalue(), "")

    def test_handover_file(self):
        # the env as given but its terminal keys; PWD the cwd, given another (as here) or none
        # (test_events_and_...)
        env = {
            "PATH": self.bin,
            "HOME": "/h",
            "CHILD_SESSION": "1",
            "PWD": "/elsewhere",
            **{k: "x" for k in tmux_host.TERMINAL_KEYS},
        }
        for argv in (("tool", "-x", "a b"), ("tool", "--settings", "s.json")):
            with self.subTest(argv=argv):
                fake = Tmux()
                self.start(fake, argv=argv, env=env)
                self.assertEqual(
                    fake._handover,
                    {
                        "argv": [self.tool, *argv[1:]],
                        "env": {
                            "PATH": self.bin,
                            "HOME": "/h",
                            "CHILD_SESSION": "1",
                            "PWD": self.cwd,
                        },
                        "cwd": self.cwd,
                    },
                )
                self.assertEqual(fake.mode, 0o600)
                self.assertEqual(fake.dir_mode, 0o700)
                self.assertEqual(os.path.dirname(os.path.dirname(fake.path)), self.temp)
                self.assertEqual(os.path.dirname(fake.fifo), os.path.dirname(fake.path))
                self.assertTrue(stat.S_ISFIFO(fake.fifo_mode))
                self.assertEqual(stat.S_IMODE(fake.fifo_mode), 0o600)
                self.assertEqual(os.listdir(self.temp), [])

    def test_terminal_keys(self):
        self.assertEqual(
            set(tmux_host.TERMINAL_KEYS),
            {
                "TMUX",
                "TMUX_PANE",
                "TERM",
                "COLORTERM",
                "TERM_PROGRAM",
                "TERM_PROGRAM_VERSION",
                "TERM_SESSION_ID",
                "ITERM_SESSION_ID",
                "ITERM_PROFILE",
                "LC_TERMINAL",
                "LC_TERMINAL_VERSION",
                "COLUMNS",
                "LINES",
            },
        )

    def test_cwd_never_reaches_tmux(self):
        self.cwd = os.path.join(self.root, "#(x)#{y}")
        os.makedirs(self.cwd)
        fake = Tmux()
        self.start(fake)
        self.assertEqual(fake._handover["cwd"], self.cwd)
        self.assertEqual([a for c in fake.calls for a in c if "#(x)" in a or "#{y}" in a], [])

    def test_refused_before_tmux(self):
        # the command resolved with the env's PATH (no sh there); an events file that is no regular
        # file
        link = os.path.join(self.root, "link")
        os.symlink(self.tool, link)
        for kw, error in (
            ({"argv": ("sh", "-c", "true")}, "sh"),
            ({"argv": ("no-such-tool",)}, "no-such-tool"),
            ({"argv": ()}, "no command"),
            ({"events": self.cwd}, ""),
            ({"events": link}, ""),
        ):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, error):
                    self.start(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_unwritable(self):
        fake = Tmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", os.path.join(self.root, "missing")),
            self.assertRaisesRegex(tmux_host.TuiError, "^handover: "),
        ):
            self.start(fake)
        real = os.open

        def refuse(path, *args, **kw):
            if str(path).endswith("handover.json"):
                raise PermissionError(13, "Permission denied", path)
            return real(path, *args, **kw)

        with (
            unittest.mock.patch("os.open", side_effect=refuse),
            self.assertRaisesRegex(tmux_host.TuiError, "^handover: "),
        ):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(self.temp), [])

    def test_relative_command_made_absolute(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        for argv0, path in (("bin/tool", "/nonexistent"), ("tool", "bin")):
            fake = Tmux()
            self.start(fake, argv=(argv0,), env={"PATH": path})
            self.assertEqual(fake._handover["argv"], [self.tool])

    def test_live_session_is_refused(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env), unittest.mock.patch.dict(os.environ, env):
                fake = Tmux(pane="0  ")
                with self.assertRaisesRegex(
                    tmux_host.TuiError,
                    "^" + re.escape(f"session s is running; {prefix}tmux attach -t '=s'") + "$",
                ):
                    self.start(fake)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_dead_session_is_killed_then_started(self):
        for pane in ("1 0 ", "1  9"):
            with self.subTest(pane=pane):
                fake = Tmux(pane=pane)
                self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "kill-session", "new-session"]
                )
                self.assertEqual(fake.calls[1], ["tmux", "kill-session", "-t", "=s"])

    def test_an_unknown_session_is_never_killed(self):
        # tmux failing otherwise: new-session finds out; another start's session is not ours to kill
        for err in FAILED:
            with self.subTest(err=err):
                fake = Tmux(results={"display-message": (1, "", err)})
                self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
        fake = Tmux(
            results={
                "display-message": (1, "", "boom\n"),
                "new-session": (1, "", "duplicate session: s\n"),
            }
        )
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: duplicate session: s$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_tags_are_session_options_set_in_the_new_session_call(self):
        fake = Tmux()
        self.start(fake, tags={"@sid": "0123abcd-x", "@k": "a b#{x}"})
        self.assertEqual(
            fake.calls[1][fake.calls[1].index(fake.fifo) + 1 :],
            [
                ";",
                "set-option",
                "-p",
                "-t",
                "=s:",
                "remain-on-exit",
                "on",
                ";",
                "set-option",
                "-t",
                "=s:",
                "@sid",
                "0123abcd-x",
                ";",
                "set-option",
                "-t",
                "=s:",
                "@k",
                "a b#{x}",
                *chained(decorations("s")),
            ],
        )
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_bad_tags_refused_before_tmux(self):
        for tags in (
            {"sid": "x"},
            {"@": "x"},
            {"@a b": "x"},
            {"@a;": "x"},
            {"@k": "x;"},
            {"@k": 1},
            {"@k": None},
        ):
            with self.subTest(tags=tags):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, "^tag "):
                    self.start(fake, tags=tags)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_a_display_tag_tmux_could_not_list_or_a_nul_is_refused_before_tmux(self):
        for tags, error in (
            ({"@display": "a\tb"}, "^display "),
            ({"@display": "x;"}, "^display "),
            ({"@k": "a\0b"}, "^tag "),
        ):
            with self.subTest(tags=tags):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, error):
                    self.start(fake, tags=tags)
                self.assertEqual(fake.calls, [])

    def test_a_failed_tmux_call_kills_the_session_it_may_have_made(self):
        fake = Tmux(
            fail={"new-session"}
        )  # e.g. the chained set-option failed after new-session made the session
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
        self.assertEqual(os.listdir(self.temp), [])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_a_duplicate_session_is_not_killed(self):
        fake = Tmux(
            results={"new-session": (1, "", "duplicate session: s\n")}
        )  # another start took the name
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: duplicate session: s$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_an_interrupt_after_new_session_kills_the_session(self):
        def interrupt(*_args, **_kw):
            raise KeyboardInterrupt

        fake = Tmux(hand_over=False)
        self.sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertIs(fake.dir_at_kill, expr2=False)
        self.assertEqual(os.listdir(self.temp), [])

    def test_tmux_missing(self):
        # status: unknown; new-session: the error
        def missing(_argv, **_kw):
            raise FileNotFoundError(2, "No such file or directory", "tmux")

        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: "):
            self.start(missing)
        self.assertEqual(os.listdir(self.temp), [])

    def test_wrapper_never_reads(self):
        for fail in ((), {"kill-session"}):
            with self.subTest(fail=fail):
                self.sleep.reset_mock()
                fake = Tmux(fail=fail, hand_over=False)
                with self.assertRaisesRegex(tmux_host.TuiError, r"^the session did not start$"):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
                self.assertIs(fake.dir_at_kill, expr2=False)
                self.assertAlmostEqual(
                    sum(c.args[0] for c in self.sleep.call_args_list), tmux_host.HANDOVER_TIMEOUT
                )
                self.assertEqual(os.listdir(self.temp), [])
                self.assertEqual(self.stderr.getvalue(), "")

    def test_waits_for_a_late_wrapper(self):
        fake = Tmux(hand_over=False)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                os.unlink(fake.path)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_waits_for_a_late_exec(self):
        fake, slept = Tmux(wrapper="hang"), []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                os.close(fake.writer)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_an_exec_failure_raises_its_error_and_kills_the_session(self):
        fake = Tmux(wrapper=errno.ENOEXEC)
        with self.assertRaisesRegex(
            tmux_host.TuiError,
            "^" + re.escape(f"cannot run {self.tool}: {os.strerror(errno.ENOEXEC)}") + "$",
        ):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_a_handover_at_its_last_poll_still_gets_the_whole_exec_wait(self):
        fake, slept = Tmux(hand_over=False), []
        last = round(tmux_host.HANDOVER_TIMEOUT / tmux_host.POLL)

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == last:
                fake.writer = os.open(fake.fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.unlink(fake.path)
            elif len(slept) == last + 3:
                os.close(fake.writer)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), last + 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_bad_errpipe_data_is_an_error_and_kills_the_session(self):
        for data in (b"x", b"9" * 20):
            with self.subTest(data=data):
                fake = Tmux(wrapper=data)
                with self.assertRaisesRegex(
                    tmux_host.TuiError,
                    "^" + re.escape(f"cannot run {self.tool}: bad errpipe data") + "$",
                ):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_never_execd_is_not_started(self):
        fake = Tmux(wrapper="hang")
        with self.assertRaisesRegex(tmux_host.TuiError, r"^the session did not start$"):
            self.start(fake)
        os.close(fake.writer)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertAlmostEqual(
            sum(c.args[0] for c in self.sleep.call_args_list), tmux_host.HANDOVER_TIMEOUT
        )
        self.assertEqual(os.listdir(self.temp), [])

    def test_unsafe_tmux_argument(self):
        for exe in ("/opt/py#3/bin/python3", "/opt/bin/python3;"):
            with self.subTest(exe=exe), unittest.mock.patch.object(sys, "executable", exe):
                fake = Tmux()
                with self.assertRaises(tmux_host.TuiError):
                    self.start(fake)
                self.assertNotIn("new-session", fake.commands())
        hashed = os.path.join(self.root, "t#mp")
        os.makedirs(hashed)
        fake = Tmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", hashed),
            self.assertRaises(tmux_host.TuiError),
        ):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(hashed), [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_invalid_session_name(self):
        for name in ("", "a b", "a.b", "a:b", "=a", "#{x}", "a;", "a\n", "é"):
            with self.subTest(name=name):
                fake = Tmux()
                for call in (
                    lambda *, fake=fake, name=name: self.start(fake, session=name),
                    lambda *, fake=fake, name=name: tmux_host.status(name, proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.detach(
                        name,
                        ["tool"],
                        proc=fake,
                        sleep=self.sleep,
                        cwd=self.cwd,
                        env={"PATH": self.bin},
                    ),
                    lambda *, fake=fake, name=name: tmux_host.kill(name, proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.send(
                        name, "x", proc=fake, sleep=self.sleep
                    ),
                    lambda *, fake=fake, name=name: tmux_host.paste(name, "x", proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.keys(name, "Enter", proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.read(name, proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.option(name, "@sid", proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.pane(name, proc=fake),
                ):
                    with self.assertRaises(tmux_host.TuiError):
                        call()
                self.assertEqual(fake.calls, [])

    def test_events_and_decorations(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        events = os.path.join(self.root, "ev")
        # the status line is the caller's choice
        for given, path, status_line, status in (
            (None, None, False, "off"),
            ("ev", events, False, "off"),
            (None, None, True, "on"),
        ):
            with self.subTest(events=given, status_line=status_line):
                fake = Tmux()
                self.start(fake, env={"PATH": self.bin}, events=given, status_line=status_line)
                self.assertEqual(fake._handover["argv"], [self.tool, "-x", "a b"])
                self.assertEqual(fake._handover["env"], {"PATH": self.bin, "PWD": self.cwd})
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
                self.assertEqual(
                    fake.calls[1][fake.calls[1].index(fake.fifo) + 8 :],
                    chained(decorations("s", path, status)),
                )
        self.assertEqual(stat.S_IMODE(os.stat(events).st_mode), 0o600)


class TileApplicationTest(TileFixture):
    def test_swaps_and_select_layout_sent_muted_then_unmuted(self):
        # manual pane %4, split from %1, follows it in pane index order; it goes last, in a second
        # column
        layout = "2cc7,160x48,0,0{79x48,0,0,0,80x48,80,0[80x8,80,0,1,80x7,80,9,4,80x15,80,17,2,80x15,80,33,3]}"
        fake = self.tile(layout, ["%0", "%1", "%4", "%2", "%3"], self.W3)
        self.assertEqual(
            fake.calls,
            [
                self.SOCKET,
                self.READ,
                self.PANES,
                SESSIONS,
                [
                    "tmux",
                    "set-option",
                    "-w",
                    "-t",
                    "@3",
                    "@grid-tiles",
                    "1000000",
                    ";",
                    "set-option",
                    "-w",
                    "-t",
                    "@3",
                    "@grid-panes",
                    "%0 %1 %2 %3 %4",
                    ";",
                    "set-option",
                    "-w",
                    "-t",
                    "@3",
                    "@grid-busy",
                    "@3",
                    ";",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%2",
                    "-t",
                    "%4",
                    ";",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%3",
                    "-t",
                    "%4",
                    ";",
                    "select-layout",
                    "-t",
                    "@3",
                    "ac6e,160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0[40x16,80,0,1,"
                    "40x15,80,17,2,40x15,80,33,3],39x48,121,0,4}}",
                ],
                ["tmux", "set-option", "-u", "-w", "-t", "@3", "@grid-busy"],
            ],
        )

    def test_a_window_split_top_bottom_before_the_grid(self):
        # the manager full width on top, a manual pane %1 below it split for w1 (%2): the manager
        # gets half
        layout = dump("160x48,0,0[160x24,0,0,0,160x11,0,25,1,160x11,0,37,2]")
        fake = self.tile(layout, ["%0", "%1", "%2"], [("$2", "w1", "mgr", "%2")])
        want = tmux_host._render_layout(grid([slot("%2"), slot("%1")]))
        self.assertEqual(
            fake.calls[4:],
            self.applied(
                [
                    "tmux",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%2",
                    "-t",
                    "%1",
                    ";",
                    "select-layout",
                    "-t",
                    "@3",
                    want,
                ]
            ),
        )
        self.assertEqual(
            want.split(",", 1)[1], "160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0,2,80x23,80,25,1]}"
        )

    def test_no_op_when_the_layouts_cells_match(self):
        fake = self.tile(self.THREE, ["%0", "%1", "%2", "%3"], self.W3)
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, SESSIONS])
        # the target's cells in another tree: a pass over the target would move them, but none
        # follows a no-op
        layout = dump(
            "200x50,0,0{80x50,0,0,0,119x50,81,0{59x50,81,0,2,29x50,141,0,1,29x50,171,0,3}}"
        )
        rows = [("$2", "w1", "old1", "%1"), ("$3", "w2", "old3", "%2"), ("$4", "w3", "w1", "%3")]
        fake = self.tile(layout, ["%0", "%2", "%1", "%3"], rows, n="1", size=(200, 50))
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, SESSIONS])

    def test_an_already_laid_out_window_records_its_pane_set_alone(self):
        # @grid-panes unset, junk or another set: set alone (pane ids in numeric order), so a later
        # --if-panes-changed tile skips a drag
        panes = ["%0", "%9", "%10"]
        layout = tmux_host._render_layout(grid([slot("%9"), slot("%10")]))
        record = ["tmux", "set-option", "-w", "-t", "@3", "@grid-panes", "%0 %9 %10"]
        for recorded in (
            "",
            "%0 %9",
            "%0 %10 %9",
            "%0 %9 %10;",
            " %0 %9 %10",
            "%0  %9 %10",
            "%0,%9,%10",
        ):
            for flag in (False, True):
                with self.subTest(recorded=recorded, if_panes_changed=flag):
                    fake = self.tile(layout, panes, recorded=recorded, if_panes_changed=flag)
                    self.assertEqual(
                        fake.calls, [self.SOCKET, self.READ, self.PANES, SESSIONS, record]
                    )
        fake = self.tile(layout, panes)
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, SESSIONS])
        with self.assertRaisesRegex(tmux_host.TuiError, "^tmux: boom$"):
            self.tile(layout, panes, recorded="", fail=("set-option",))

    def test_if_panes_changed_skips_while_the_pane_set_is_the_recorded_one(self):
        # a drag or resize keeps the pane set: no pass, nothing sent; without the flag the drag
        # snaps back
        fake = self.tile(*self.WIDE, if_panes_changed=True)
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES])
        fake = self.tile(*self.WIDE)
        self.assertEqual(
            fake.calls,
            [self.SOCKET, self.READ, self.PANES, SESSIONS, *self.applied(self.WIDE_SENT)],
        )
        # another set (a pane opened or closed), or one unset or junk: the pass runs and records the
        # new set
        for recorded in ("", "%0 %1", "%0 %1 %2 %3", "%0 %2 %1", "%0 %1 %2;", "%0 %1 %2\x00", "x"):
            with self.subTest(recorded=recorded):
                fake = self.tile(*self.WIDE, recorded=recorded, if_panes_changed=True)
                self.assertEqual(
                    fake.calls,
                    [self.SOCKET, self.READ, self.PANES, SESSIONS, *self.applied(self.WIDE_SENT)],
                )

    def test_if_panes_changed_reads_only_holding_the_lock(self):
        release = hold(self, self.lock)
        fake = self.tile(*self.WIDE, recorded="", wait=False, if_panes_changed=True)
        self.assertEqual(fake.calls, [self.SOCKET])
        release()
        fake = self.tile(*self.WIDE, recorded="", wait=False, if_panes_changed=True)
        self.assertEqual(
            fake.calls,
            [self.SOCKET, self.READ, self.PANES, SESSIONS, *self.applied(self.WIDE_SENT)],
        )

    def test_if_panes_changed_zoomed_no_grid_or_torn_down_records_nothing(self):
        for kw in ({"zoomed": "1"}, {"manager": ""}):
            with self.subTest(**kw):
                fake = self.tile(*self.WIDE, recorded="", **kw, if_panes_changed=True)
                self.assertEqual(fake.calls, [self.SOCKET, self.READ])
        fake = self.tile(
            dump("160x48,0,0[160x16,0,0,1,160x15,0,17,2,160x15,0,33,3]"),
            ["%1", "%2", "%3"],
            self.W3,
            recorded="%0 %1 %2 %3",
            if_panes_changed=True,
        )
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, self.TEARDOWN])
        fake = self.tile(
            "bogus", ["%0", "%1", "%2", "%3"], self.W3, recorded="", if_panes_changed=True
        )
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES])

    def test_a_state_needing_several_passes_gets_its_fixed_point_at_once(self):
        # 200x50, orphans of gone openers in an old layout: 2 and 3 passes each, a pass over the
        # result sends nothing
        for n, layout, panes, rows, swap, want in (
            (
                "1",
                "200x50,0,0[200x25,0,0{100x25,0,0,2,99x25,101,0,1},200x24,0,26,0]",
                ["%2", "%1", "%0"],
                [("$2", "w1", "old1", "%1"), ("$3", "w2", "old2", "%2")],
                ("%0", "%2"),
                "200x50,0,0{99x50,0,0,0,100x50,100,0{50x50,100,0,1,49x50,151,0,2}}",
            ),
            (
                "2",
                "200x50,0,0[200x16,0,0,0,200x16,0,17,3,200x16,0,34{100x16,0,34,2,99x16,101,34,1}]",
                ["%0", "%3", "%2", "%1"],
                [("$2", "w1", "old2", "%1"), ("$3", "w2", "old1", "%2"), ("$4", "w3", "w2", "%3")],
                ("%2", "%3"),
                (
                    "200x50,0,0{99x50,0,0,0,100x50,100,0[100x25,100,0{50x25,100,0,2,49x25,151,0,3},100x24,100,26,1]}"
                ),
            ),
        ):
            with self.subTest(n=n):
                fake = self.tile(dump(layout), panes, rows, n=n, size=(200, 50))
                self.assertEqual(
                    fake.calls[4:],
                    self.applied(
                        [
                            "tmux",
                            "swap-pane",
                            "-d",
                            "-s",
                            swap[0],
                            "-t",
                            swap[1],
                            ";",
                            "select-layout",
                            "-t",
                            "@3",
                            dump(want),
                        ]
                    ),
                )
                after = [leaf.pane for leaf in tmux_host._leaves(tmux_host._parse_layout(want))]
                fake = self.tile(dump(want), after, rows, n=n, size=(200, 50))
                self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, SESSIONS])

    def test_the_pass_only_reads_and_returns_grid_tiles_as_read_with_what_to_send(self):
        split = "2cc7,160x48,0,0{79x48,0,0,0,80x48,80,0[80x8,80,0,1,80x7,80,9,4,80x15,80,17,2,80x15,80,33,3]}"
        for layout, panes, zoomed, want, reads in (
            (
                split,
                ["%0", "%1", "%4", "%2", "%3"],
                "0",
                (
                    [("%2", "%4"), ("%3", "%4")],
                    tmux_host._parse_layout(tmux_host._render_layout(grid(4))),
                    "1 x 2",
                    "%0 %1 %2 %3 %4",
                ),
                [self.READ, self.PANES, SESSIONS],
            ),
            (self.THREE, ["%0", "%1", "%2", "%3"], "0", None, [self.READ, self.PANES, SESSIONS]),
            (self.THREE, ["%0", "%1", "%2", "%3"], "1", None, [self.READ]),
        ):
            with self.subTest(layout=layout, zoomed=zoomed):
                fake = self.fake(layout, panes, self.W3, zoomed=zoomed, tiles="1 x 2")
                self.assertEqual(
                    tmux_host._grid_pass("@3", None, fake, if_panes_changed=False), want
                )
                self.assertEqual(fake.calls, reads)

    def test_a_dead_worker_keeps_its_cell(self):
        # FR-12: w2's command exited (its session and dead pane stay), its host pane %2 attaches on;
        # w1 (%1) closed
        layout = dump("160x48,0,0{79x48,0,0,0,80x48,80,0[80x32,80,0,2,80x15,80,33,3]}")
        fake = self.tile(layout, ["%0", "%2", "%3"], self.W3[1:])
        self.assertEqual(
            fake.calls[4:],
            self.applied(
                [
                    "tmux",
                    "select-layout",
                    "-t",
                    "@3",
                    tmux_host._render_layout(grid([slot("%2"), slot("%3")])),
                ]
            ),
        )
        self.assertNotIn("pane_dead", repr(fake.calls))

    def test_zoomed_or_no_grid_window_only_reads_it(self):
        for kw in ({"zoomed": "1"}, {"manager": ""}):
            with self.subTest(**kw):
                fake = self.tile(*self.WIDE, **kw)
                self.assertEqual(fake.calls, [self.SOCKET, self.READ])

    def test_teardown_when_the_manager_is_gone_invalid_or_alone(self):
        for manager, layout, panes in (
            (
                "%0",
                dump("160x48,0,0[160x16,0,0,1,160x15,0,17,2,160x15,0,33,3]"),
                ["%1", "%2", "%3"],
            ),
            ("0", self.THREE, ["%0", "%1", "%2", "%3"]),
            ("%0;", self.THREE, ["%0", "%1", "%2", "%3"]),
            ("%0", dump("160x48,0,0,0"), ["%0"]),
        ):
            with self.subTest(manager=manager, panes=panes):
                fake = self.tile(layout, panes, self.W3, manager=manager)
                self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, self.TEARDOWN])

    def test_nothing_changed_when_the_layout_is_unreadable_or_not_the_windows_panes(self):
        four = ["%0", "%1", "%2", "%3"]
        for layout, panes in (
            ("bogus", four),
            (self.THREE[:-1], four),
            ("0000" + self.THREE[4:], four),
            (self.THREE, four[:3]),
            (self.THREE, [*four, "%4"]),
            (self.THREE, [*four[:3], "%5"]),
            (dump("160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0,1,80x23,80,25,1]}"), four[:2]),
        ):
            with self.subTest(layout=layout, panes=panes):
                fake = self.tile(layout, panes, self.W3)
                self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES])

    def test_too_small_nothing_changed(self):
        # three cells side by side, 4 high: a column of three needs 5
        layout = dump("160x4,0,0{79x4,0,0,0,80x4,80,0{26x4,80,0,1,26x4,107,0,2,26x4,134,0,3}}")
        fake = self.tile(layout, ["%0", "%1", "%2", "%3"], self.W3, size=(160, 4))
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, SESSIONS])

    def test_a_later_pass_too_small_keeps_the_last_that_fits(self):
        layout = dump(
            "116x11,0,0{51x11,0,0,0,64x11,52,0[64x3,52,0{32x3,52,0,3,31x3,85,0,1},64x3,52,4{32x3,52,4,4,"
            "31x3,85,4,5},64x3,52,8{32x3,52,8,6,31x3,85,8,2}]}"
        )
        rows = [
            ("$2", "w1", "mgr", "%1"),
            ("$3", "w2", "old1", "%2"),
            ("$4", "w3", "mgr", "%3"),
            ("$5", "w4", "w2", "%4"),
            ("$6", "w5", "old1", "%5"),
            ("$7", "w6", "mgr", "%6"),
        ]
        fake = self.tile(
            layout, ["%0", "%3", "%1", "%4", "%5", "%6", "%2"], rows, n="4", size=(116, 11)
        )
        self.assertEqual(
            fake.calls[4:],
            self.applied(
                [
                    "tmux",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%1",
                    "-t",
                    "%3",
                    ";",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%2",
                    "-t",
                    "%3",
                    ";",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%3",
                    "-t",
                    "%5",
                    ";",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%5",
                    "-t",
                    "%6",
                    ";",
                    "select-layout",
                    "-t",
                    "@3",
                    "6fdc,116x11,0,0{51x11,0,0,0,64x11,52,0{32x11,52,0[32x2,52,0,1,32x2,52,3{"
                    "16x2,52,3,2,15x2,69,3,4},32x2,52,6,3,32x2,52,9,5],31x11,85,0,6}}",
                ]
            ),
        )

    def test_duplicate_pane_claims_the_lower_session_id_owns(self):
        rows = [
            ("$5", "x", "mgr", "%1"),
            ("$3", "y", "mgr", "%1"),
            ("$6", "z", "x", "%2"),
            ("$7", "w", "y", "%3"),
        ]
        fake = self.tile(self.THREE, ["%0", "%1", "%2", "%3"], rows)
        self.assertEqual(
            fake.calls[4:],
            self.applied(
                [
                    "tmux",
                    "swap-pane",
                    "-d",
                    "-s",
                    "%3",
                    "-t",
                    "%2",
                    ";",
                    "select-layout",
                    "-t",
                    "@3",
                    (
                        "9a41,160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0{40x24,80,0,1,39x24,121,0,3},80x23,80,25,2]}"
                    ),
                ]
            ),
        )

    def test_session_fields_read_back_only_after_a_fullmatch(self):
        # a row with a bad id or name is skipped (its pane a manual pane); an @opener that is no
        # opener is unset
        rows = [
            ("$2", "w1", "mgr", "%1"),
            ("2", "w2", "mgr", "%2"),
            ("$4", "w 4", "mgr", "%4"),
            ("$5", "w5", "w1", "%3"),
            ("$6", "w6", "%1", "%5"),
        ]
        panes = ["%0", "%1", "%2", "%3", "%4", "%5"]
        fake = self.tile(
            tmux_host._render_layout(grid([slot(p) for p in panes[1:]], 6)), panes, rows, n="6"
        )
        cells = [slot("%1", slot("%3")), slot("%5"), slot("%2"), slot("%4")]
        self.assertEqual(fake.calls[4][-1], tmux_host._render_layout(grid(cells, 6)))

    def test_per_column_argument_else_the_window_option_else_the_default(self):
        panes = ["%0", "%1", "%2", "%3", "%4"]
        rows = [(f"${i + 1}", f"w{i}", "mgr", f"%{i}") for i in range(1, 5)]
        for n, kw, per_column in (
            ("2", {}, 2),
            ("9999", {}, 9999),
            ("1", {"per_column": 2}, 2),
            ("2", {"per_column": 3}, None),
            ("", {}, None),
            ("0", {}, None),
            ("02", {}, None),
            ("10000", {}, None),
            ("2 ", {}, None),
            ("x", {}, None),
        ):
            with self.subTest(n=n, **kw):
                fake = self.tile(tmux_host._render_layout(grid(4)), panes, rows, n=n, **kw)
                want = tmux_host._render_layout(grid(4, per_column)) if per_column else None
                self.assertEqual(
                    fake.calls[4:],
                    self.applied(["tmux", "select-layout", "-t", "@3", want]) if want else [],
                )


class TileCoordinationTest(TileFixture):
    def test_a_hook_run_returns_at_once_while_another_tile_holds_the_lock(self):
        release = hold(self, self.lock)
        fake = self.tile(*self.WIDE, wait=False)
        self.assertEqual(fake.calls, [self.SOCKET])
        release()
        fake = self.tile(*self.WIDE, wait=False)
        self.assertEqual(
            fake.calls,
            [self.SOCKET, self.READ, self.PANES, SESSIONS, *self.applied(self.WIDE_SENT)],
        )

    def test_an_explicit_tile_waits_for_the_lock_then_tiles(self):
        release = hold(self, self.lock)
        fake, seen = self.fake(*self.WIDE), []

        def poll(sleeps):
            seen.append(list(fake.calls))
            if sleeps == 3:
                release()

        slept = clock(self, poll)
        tmux_host.tile("@3", proc=fake)
        self.assertEqual(slept, [tmux_host.TILE_POLL] * 3)
        self.assertEqual(seen, [[self.SOCKET]] * 3)
        self.assertEqual(
            fake.calls,
            [
                self.SOCKET,
                self.READ,
                self.PANES,
                SESSIONS,
                *self.applied(self.WIDE_SENT, str(int(time.monotonic() * 1000))),
            ],
        )

    def test_an_explicit_tile_gives_up_after_tile_wait(self):
        self.assertEqual((tmux_host.TILE_POLL, tmux_host.TILE_WAIT), (0.05, 5))
        hold(self, self.lock)
        slept = clock(self)
        fake = self.fake(*self.WIDE)
        with self.assertRaisesRegex(tmux_host.TuiError, "^grid @3: busy$"):
            tmux_host.tile("@3", proc=fake)
        self.assertEqual(fake.calls, [self.SOCKET])
        self.assertEqual(set(slept), {tmux_host.TILE_POLL})
        self.assertTrue(5 <= sum(slept) < 5.1)  # float steps: one poll more

    def test_a_symlink_at_the_lock_path_raises_before_the_pass_and_grid_up_returns_it(self):
        os.symlink(self.sock + "-target", self.lock)
        want = f"^grid lock {re.escape(self.lock)}: {os.strerror(errno.ELOOP)}$"
        fake = self.fake(*self.WIDE)
        with self.assertRaisesRegex(tmux_host.TuiError, want):
            tmux_host.tile("@3", proc=fake)
        self.assertEqual(fake.calls, [self.SOCKET])
        fake = self.fake(*self.WIDE)
        with (
            unittest.mock.patch.object(sys, "executable", PY),
        ):
            self.assertRegex(
                tmux_host._grid_up(
                    "@3", "%0", per_column=3, retile="all", proc=fake, script=SCRIPT
                ),
                want,
            )
        self.assertEqual(fake.calls, [grid_set("@3", "%0"), self.SOCKET])
        self.assertFalse(os.path.lexists(self.sock + "-target"))

    def test_an_empty_or_relative_socket_path_raises_before_the_lock(self):
        # the lock would land in the cwd, here an empty temp dir
        cwd = tempfile.TemporaryDirectory()
        self.addCleanup(cwd.cleanup)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(cwd.name)
        for sock in ("", "default"):
            with self.subTest(sock=sock):
                fake = self.fake(*self.WIDE, sock=sock)
                with self.assertRaisesRegex(
                    tmux_host.TuiError,
                    f"^grid @3: socket path {re.escape(repr(sock))}: want an absolute path$",
                ):
                    tmux_host.tile("@3", proc=fake)
                self.assertEqual(fake.calls, [self.SOCKET])
                self.assertEqual(os.listdir(cwd.name), [])

    def test_a_fifo_at_the_lock_path_does_not_hang(self):
        os.mkfifo(self.lock, 0o600)
        with deadline(), suppress(tmux_host.TuiError):  # flock on a FIFO: ENOTSUP on macOS
            self.tile(*self.WIDE)

    def test_grid_tiles_keeps_the_re_tiles_of_the_last_tile_window_then_adds_now(self):
        # now 1000000 ms: the window is 990001..1000000; junk, stale and future times are dropped
        junk = [
            "x",
            "-999999",
            "+999999",
            "999999.0",
            "1e6",
            "\uff19\uff19\uff19",
            "0000000000999999",
            "",
        ]
        tiles = " ".join(
            [
                *["990000", "5", "0"] * 50,
                *junk,
                "990001",
                "1000001",
                "999999999999999",
                "000000000999998",
                "999999\t1000000",
            ]
        )
        fake = self.tile(*self.WIDE, tiles=tiles)
        self.assertEqual(
            fake.calls[4:], self.applied(self.WIDE_SENT, "990001 999998 999999 1000000 1000000")
        )
        fake = self.tile(self.THREE, ["%0", "%1", "%2", "%3"], self.W3, tiles=tiles)
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, SESSIONS])

    def test_more_than_tile_limit_re_tiles_in_tile_window_tear_the_grid_down(self):
        self.assertEqual((tmux_host.TILE_LIMIT, tmux_host.TILE_WINDOW), (50, 10))
        line = "tui: grid @3: auto re-tiling stopped: 51 re-tiles in 10 s; the next worker that opens rebuilds it"
        recent = [str(990001 + i * 199) for i in range(50)]
        for message in ((0,), (1, "", "no client\n"), OSError("gone")):
            with self.subTest(message=message):
                err = io.StringIO()
                with redirect_stderr(err):
                    fake = self.tile(*self.WIDE, tiles=" ".join(recent), message=message)
                self.assertEqual(
                    fake.calls,
                    [
                        self.SOCKET,
                        self.READ,
                        self.PANES,
                        SESSIONS,
                        self.TEARDOWN,
                        ["tmux", "display-message", "-d", "10000", "-t", "@3", line],
                    ],
                )
                self.assertEqual(err.getvalue(), line + "\n")
        fake = self.tile(*self.WIDE, tiles=" ".join(recent[1:]))
        self.assertEqual(
            fake.calls[4:], self.applied(self.WIDE_SENT, " ".join([*recent[1:], "1000000"]))
        )

    def test_after_a_teardown_grid_up_under_off_sets_no_hook(self):
        recent = " ".join(str(990001 + i * 199) for i in range(50))
        with redirect_stderr(io.StringIO()):
            fake = self.tile(*self.WIDE, tiles=recent)
        self.assertEqual(fake.calls[4], self.TEARDOWN)
        del fake.calls[:]
        with (
            unittest.mock.patch.object(sys, "executable", PY),
            contextlib.nullcontext(unittest.mock.Mock()) as tile,
        ):
            self.assertIsNone(
                tmux_host._grid_up(
                    "@3",
                    "%0",
                    per_column=3,
                    retile="off",
                    proc=fake,
                    script=SCRIPT,
                    tile_window=tile,
                )
            )
        self.assertEqual(fake.calls, [grid_set("@3", "%0", retile="off")])
        tile.assert_not_called()

    def test_bad_window_or_per_column_raises_before_tmux(self):
        cases = [
            ("3", {}, "invalid window id '3': want @[0-9]+"),
            *((window, {}, "invalid window id") for window in ("@3;", "%3", "@", " @3", "@1a")),
            ("@3", {"per_column": 0}, "per_column must be an int from 1 to 9999, not 0"),
            *(("@3", {"per_column": n}, "per_column") for n in (10000, True, "3", 2.0)),
        ]
        for window, kw, error in cases:
            with self.subTest(window=window, **kw):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, "^" + re.escape(error)):
                    tmux_host.tile(window, proc=fake, **kw)
                self.assertEqual(fake.calls, [])

    def test_a_tmux_failure_raises(self):
        # a failed re-tile: @grid-busy unset all the same, its failure not masking the re-tile's;
        # the lock freed
        fail = {"set-option": lambda argv: (1, "", "boom\n" if "@grid-tiles" in argv else "gone\n")}
        fake = self.fake(*self.WIDE, **fail)
        with self.assertRaisesRegex(tmux_host.TuiError, "^tmux: boom$"):
            tmux_host.tile("@3", proc=fake)
        self.assertEqual(fake.calls[4:], self.applied(self.WIDE_SENT))
        with tmux_host._grid_lock(self.sock, "@3", wait=False) as held:
            self.assertTrue(held)
        with self.assertRaisesRegex(tmux_host.TuiError, "^tmux: boom$"):
            self.tile(dump("160x48,0,0,0"), ["%0"], fail=("set-hook",))
        with self.assertRaisesRegex(tmux_host.TuiError, "^tmux: boom$"):
            tmux_host.tile("@3", proc=Tmux(fail=("display-message",)))
