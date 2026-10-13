"""Fresh tmux launch, pane and grid dependencies for behavior tests."""

import io
import os
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr
from dataclasses import dataclass

from agent_pm.session.hosts.tmux import tmux_host

from session.tests.case import Case
from session.tests.unit.tmux.support import (
    SOCK,
    TMUX,
    Tmux,
    clock,
    environ,
    grid_set,
    record,
    which,
)


@dataclass(frozen=True)
class GridSnapshot:
    layout: str
    panes: tuple | list
    rows: tuple | list = ()
    manager: str = "%0"
    n: str = ""
    zoomed: str = "0"
    size: tuple = (160, 48)
    tiles: str = ""
    sock: str | None = None
    recorded: str | None = None


@dataclass(frozen=True, kw_only=True)
class GridOpening:
    window: str = "@1"
    manager: str = "%3"
    opener: str = "mgr"
    n: int = 3
    retile: str = "all"
    hooks: bool = True


def opening_commands(facts: GridOpening | None = None):
    facts = facts or GridOpening()
    return [
        *record(facts.opener, "%20"),
        grid_set(facts.window, facts.manager, facts.n, hooks=facts.hooks, retile=facts.retile),
        *([] if facts.retile == "off" else [["tile", facts.window, facts.n]]),
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
                session,
                tmux_host.Command(argv=list(argv), cwd=self.cwd, env=env),
                options=tmux_host.StartOptions(**kw),
                proc=fake,
                sleep=self.sleep,
            )


class PaneFixture(Case):
    def show(self, fake, env, **kw):
        self.stderr = io.StringIO()
        with environ(**env), which(kw.pop("tmux", TMUX)), redirect_stderr(self.stderr):
            return tmux_host.show(
                "s",
                kw.pop("template", None),
                options=tmux_host.PaneOptions(**kw),
                services=tmux_host.PaneServices(proc=fake, script=tmux_host.__file__),
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
            "#{window_width}\t#{window_height}\t#{window_zoomed_flag}\t"
            "#{window_layout}\t#{@grid-manager}\t#{@grid-per-column}\t"
            "#{@grid-panes}\t#{@grid-tiles}"
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

    def fake(self, snapshot: GridSnapshot, *, fail=(), message=(0,), **results):
        """A tmux printing these for @3, its socket `sock` (default self.sock), `message` the result
        of another display-message; rows: (session id, name, @opener, @pane) besides mgr's;
        recorded: @grid-panes (None: panes' pane set, kept as self.set).
        """
        layout, panes, rows, manager, n, zoomed, size, tiles, sock, recorded = (
            snapshot.layout,
            snapshot.panes,
            snapshot.rows,
            snapshot.manager,
            snapshot.n,
            snapshot.zoomed,
            snapshot.size,
            snapshot.tiles,
            snapshot.sock,
            snapshot.recorded,
        )
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
        self, snapshot: GridSnapshot, *, per_column=None, wait=True, if_panes_changed=False, **kw
    ):
        """tile @3 given what tmux prints (fake's arguments). Returns the fake."""
        fake = self.fake(snapshot, **kw)
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
