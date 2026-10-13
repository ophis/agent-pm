"""session.engine.api.show and close on a private tmux server (session/tests/live_tmux.py), placing
sessions as hosts/tmux/tmux_host.py lays them out: the grid (columns, name order, sub-sessions,
an unshown session reopened), its settling and re-tiling modes, and a split outside it; ported
from main's crew integration tests. Sessions are tmux_host.start's, shown from a session's pane
(os.environ that pane's env) or from outside tmux; the iTerm2 path fails the test. Also start's
dead line for a command ending at once."""

import contextlib
import functools
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_pm.session.engine.types import DriverError
from agent_pm.session.hosts.tmux import tmux_host

from session.tests import live_tmux
from session.tests.case import Case
from session.tests.processes import reject_command

MANAGER = "mgr"
TIMEOUT = 60
TUI = os.path.abspath(tmux_host.__file__)
IGNORED = "tui: show: grid: --split/--split-from ignored"
STEADY = 1  # seconds a check that nothing re-tiles holds
BREAKER_TIMEOUT = 30
RETILE_WAIT = 0.5
PACE = 0.02


class Live(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        self.server.attach(MANAGER)
        patch = mock.patch.dict(os.environ, self.server.env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        patch = reject_command("osascript")
        patch.start()
        self.addCleanup(patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = os.path.realpath(tmp.name)
        self.settings = {}
        self.configure()

    def configure(self, **settings) -> None:
        """The tui settings show() reads: these, over the earlier ones."""
        self.settings.update(settings)
        self.configure_session({}, self.settings)

    def per_column(self, n: int) -> None:
        self.configure(workers_per_column=n)

    def retile(self, mode: str) -> None:
        self.configure(grid_retile=mode)

    @contextlib.contextmanager
    def inside(self, session: str | None):
        """os.environ as a process's in `session`'s pane; None: outside tmux."""
        with mock.patch.dict(os.environ, self.server.inside(session) if session else {}):
            yield

    def show(self, name: str, inside: str | None = MANAGER, **kwargs) -> str:
        """show(name, **kwargs) from inside `inside`, a pane then showing it; its stderr."""
        err = io.StringIO()
        with self.inside(inside), contextlib.redirect_stderr(err):
            self.assertIsNone(self.driver.show(name, **kwargs))
        return err.getvalue()

    def started(self, name: str, inside: str | None = MANAGER, **kwargs) -> str:
        """Session `name` started with no pane, then show()n; show's stderr."""
        tmux_host.start(name, list(live_tmux.IDLE), cwd=self.cwd, env=self.server.env())
        return self.show(name, inside, **kwargs)

    def tmux(self, *args: str) -> str:
        res = self.server.tmux(*args)
        self.assertEqual(res.returncode, 0, f"tmux {args}: {res.stderr}")
        return res.stdout.removesuffix("\n")

    def tui(self, *args: str) -> None:
        """tmux_host.py's CLI, run from inside the manager's pane."""
        res = subprocess.run(
            [sys.executable, "-I", TUI, *args],
            env=self.server.env(**self.server.inside(MANAGER)),
            cwd=self.cwd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def display(self, name: str, fmt: str) -> str:
        return self.tmux("display-message", "-p", "-t", f"={name}:", fmt)

    def window_option(self, name: str, key: str) -> str:
        return self.tmux("show-options", "-wqv", "-t", f"={name}:", key)

    def window_of(self, name: str) -> str:
        return self.display(name, "#{window_id}")

    def grid(self, columns: list, manager_width: int | None = None) -> None:
        check = functools.partial(
            live_tmux.assert_grid, self, self.server, MANAGER, columns, manager_width
        )
        with contextlib.suppress(
            AssertionError
        ):  # on timeout, check's own message below rather than wait's repr
            live_tmux.wait(check)
        check()

    def hooks(self, name: str) -> list[str]:
        out = self.tmux("show-hooks", "-w", "-t", self.window_of(name))
        return [h for h in tmux_host.GRID_HOOKS if re.search(rf"^{h}\b", out, re.M)]

    def open_close_hooks(self, name: str) -> None:
        self.assertEqual(self.hooks(name), list(tmux_host.RETILE_HOOKS["open-close"]))
        out = self.tmux("show-hooks", "-w", "-t", self.window_of(name))
        for h in tmux_host.RETILE_HOOKS["open-close"]:
            line = next(line for line in out.splitlines() if re.match(rf"{h}\b", line))
            self.assertIn(" tile --no-wait --if-panes-changed @", line)

    def pane_set(self, name: str) -> str:
        panes = self.tmux("list-panes", "-t", self.window_of(name), "-F", "#{pane_id}").split()
        return " ".join(sorted(panes, key=lambda p: int(p[1:])))

    def holds(self, read, value, what: str):
        end = time.monotonic() + STEADY
        while time.monotonic() < end:
            self.assertEqual(read(), value, what)
            time.sleep(live_tmux.POLL)
        return value


class GridTest(Live):
    def setUp(self):
        super().setUp()
        self.retile("all")

    def test_columns_retile(self):
        self.per_column(2)
        # tmux's split leaves the split pane the wider half (100 of 200); tile keeps the manager's width
        width = self.server.size[0] // 2
        self.started("w1")
        self.grid([["w1"]], width)
        self.assertEqual(self.window_option(MANAGER, "@grid-per-column"), "2")
        self.assertIn(IGNORED, self.started("w2", split="below").splitlines())
        self.grid([["w1", "w2"]], width)
        self.started("w3")
        self.grid([["w1", "w2"], ["w3"]], width)
        self.driver.close("w2")
        self.grid([["w1", "w3"]], width)

    def test_name_order(self):
        self.per_column(2)
        for name, columns in (
            ("pm-TASK-10", [["pm-TASK-10"]]),
            ("engineer-TASK-2", [["engineer-TASK-2", "pm-TASK-10"]]),
            ("pm-TASK-9", [["engineer-TASK-2", "pm-TASK-9"], ["pm-TASK-10"]]),
            (
                "engineer-TASK-11",
                [["engineer-TASK-2", "engineer-TASK-11"], ["pm-TASK-9", "pm-TASK-10"]],
            ),
        ):
            self.started(name)
            self.grid(columns)

    def test_sub_sessions(self):
        for i, name in enumerate(("m1", "m2", "m3"), 1):
            self.started(name)
            self.grid([[f"m{k}" for k in range(1, i + 1)]])
        self.started("z1", inside="m2")
        self.grid([["m1", ("m2", ["z1"]), "m3"]])
        self.started("z2", inside="m2")
        self.grid([["m1", ("m2", ["z1", "z2"]), "m3"]])
        self.driver.close("z1")
        self.driver.close("z2")
        self.grid([["m1", "m2", "m3"]])
        self.started("z3", inside="m2")
        self.grid([["m1", ("m2", ["z3"]), "m3"]])
        self.driver.close("m2")
        self.grid([["m1", ["z3"], "m3"]])

    def test_an_unshown_session_reopens_in_the_grid(self):
        self.started("w1")
        self.grid([["w1"]])
        closed = self.server.pane_of("w1")
        self.tmux("kill-pane", "-t", closed)
        live_tmux.wait(
            lambda: not self.tmux("list-clients", "-t", "=w1"), what="w1 shown by no client"
        )
        self.assertEqual(self.show("w1"), "")
        self.assertNotEqual(self.server.pane_of("w1"), closed)
        self.grid([["w1"]])
        self.assertEqual(self.server.option("w1", "@opener"), MANAGER)


class SettleTest(Live):
    def setUp(self):
        super().setUp()
        self.retile("all")
        self.manager = self.server.inside(MANAGER)["TMUX_PANE"]
        self.window = self.display(MANAGER, "#{window_id}")
        w1 = self.tmux(
            "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", self.manager, *live_tmux.IDLE
        )
        w2 = self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}", "-t", w1, *live_tmux.IDLE)
        for name, opener, pane in (("w1", "old1", w1), ("w2", "old2", w2)):
            self.tmux("new-session", "-d", "-s", name, *live_tmux.IDLE)
            self.tmux(
                "set-option",
                "-t",
                f"={name}:",
                "@opener",
                opener,
                ";",
                "set-option",
                "-t",
                f"={name}:",
                "@pane",
                pane,
            )
        self.tmux("swap-pane", "-d", "-s", self.manager, "-t", w2)
        self.assertEqual(
            self.tmux("list-panes", "-t", self.window, "-F", "#{pane_id}").split(),
            [w2, w1, self.manager],
        )
        old = tmux_host._render_layout(
            tmux_host._parse_layout(
                f"200x50,0,0[200x25,0,0{{100x25,0,0,{w2[1:]},99x25,101,0,{w1[1:]}}},200x24,0,26,{self.manager[1:]}]"
            )
        )
        self.tmux("select-layout", "-t", self.window, old)
        self.assertEqual(self.layout(), old)

    def layout(self) -> str:
        return self.tmux("display-message", "-p", "-t", self.window, "#{window_layout}")

    def grid_up(self) -> None:
        with self.inside(MANAGER):
            self.assertIsNone(
                tmux_host._grid_up(self.window, self.manager, 1, "all", subprocess.run)
            )

    def settled(self) -> str:
        self.grid([[["w1"]], [["w2"]]], 99)
        return self.steady(self.layout())

    def steady(self, layout: str) -> str:
        return self.holds(self.layout, layout, "the window's layout")

    def hand_layout(self, tiles: str) -> tuple[str, str]:
        # even-vertical: even-horizontal of these three panes already is their grid
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        end = time.monotonic() + RETILE_WAIT
        state = f"#{{{tmux_host.GRID_MANAGER}}}\t#{{{tmux_host.GRID_BUSY}}}\t#{{{tmux_host.GRID_TILES}}}"
        while True:
            manager, busy, now = self.tmux("display-message", "-p", "-t", self.window, state).split(
                "\t"
            )
            if not manager or now != tiles and not busy or time.monotonic() > end:
                return manager, now
            time.sleep(PACE)

    def test_grid_up_settles(self):
        self.grid_up()
        self.settled()

    def test_concurrent_tiles_settle(self):
        self.tmux(
            "set-option",
            "-w",
            "-t",
            self.window,
            tmux_host.GRID_MANAGER,
            self.manager,
            ";",
            "set-option",
            "-w",
            "-t",
            self.window,
            tmux_host.GRID_PER_COLUMN,
            "1",
        )
        env = self.server.env(**self.server.inside(MANAGER))
        tiles = [
            subprocess.Popen(
                [sys.executable, "-I", TUI, "tile", *flag, self.window],
                env=env,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for flag in (["--no-wait"], [])
            for _ in range(8)
        ]
        for p in tiles:
            self.addCleanup(
                p.communicate
            )  # cleanups run last first: after the kill, reaps it, closes its pipes
            self.addCleanup(p.kill)
        for p in tiles:
            _, err = p.communicate(timeout=TIMEOUT)
            self.assertEqual(p.returncode, 0, f"{p.args}: {err}")
        self.settled()

    def test_busy_mutes_the_hooks(self):
        self.grid_up()
        grid = self.settled()
        self.tmux("set-option", "-w", "-t", self.window, tmux_host.GRID_BUSY, self.window)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.assertNotEqual(self.steady(self.layout()), grid)
        self.tmux("set-option", "-u", "-w", "-t", self.window, tmux_host.GRID_BUSY)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.settled()

    def test_breaker_stops_a_loop(self):
        self.grid_up()
        self.settled()
        start, seen, most = time.monotonic(), 0, 0
        tiles = self.window_option(MANAGER, tmux_host.GRID_TILES)
        while True:
            manager, now = self.hand_layout(tiles)
            if not manager:
                break
            seen, most, tiles = seen + (now != tiles), max(most, len(now.split())), now
            elapsed = time.monotonic() - start
            self.assertLess(
                elapsed,
                BREAKER_TIMEOUT,
                f"still a grid after {elapsed:.0f} s: {seen} re-tiles seen, {seen / elapsed:.1f}/s; it "
                f"trips past {tmux_host.TILE_LIMIT} in {tmux_host.TILE_WINDOW} s",
            )
        # the trip follows TILE_LIMIT re-tiles; the loop's last look may miss the last one
        self.assertGreaterEqual(most, tmux_host.TILE_LIMIT - 1)
        options = (
            tmux_host.GRID_MANAGER,
            tmux_host.GRID_PER_COLUMN,
            tmux_host.GRID_TILES,
            tmux_host.GRID_BUSY,
            tmux_host.GRID_PANES_SET,
        )
        self.assertEqual([self.window_option(MANAGER, k) for k in options], [""] * len(options))
        hooks = self.tmux("show-hooks", "-w", "-t", self.window)
        self.assertEqual([h for h in tmux_host.GRID_HOOKS if h in hooks], [], hooks)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.steady(self.layout())
        self.started("w3")
        self.grid([[["w1"], ["w2"], "w3"]])


class RetileTest(Live):
    def setUp(self):
        super().setUp()
        self.manager = self.server.inside(MANAGER)["TMUX_PANE"]

    def panes(self) -> dict[str, tuple[int, int, int, int]]:
        return self.server.panes(f"={MANAGER}:")

    def layout(self) -> str:
        return self.display(MANAGER, "#{window_layout}")

    def tiles(self) -> str:
        return self.window_option(MANAGER, tmux_host.GRID_TILES)

    def drag(self, name: str, rows: int) -> str:
        before = self.layout()
        self.tmux(
            "resize-pane",
            "-t",
            self.server.pane_of(name),
            "-D" if rows > 0 else "-U",
            str(abs(rows)),
        )
        after = self.layout()
        self.assertNotEqual(after, before)
        return after

    def split_of(self, before: dict, name: str, target: str, side: str) -> None:
        after, new = self.panes(), self.server.pane_of(name)
        self.assertEqual(set(after), {*before, new})
        self.assertEqual(
            {p: after[p] for p in before if p != target},
            {p: before[p] for p in before if p != target},
        )
        (x, y, w, h), kept, added = before[target], after[target], after[new]
        if side == "right":
            want = (x, y, kept[2], h), (x + kept[2] + 1, y, w - kept[2] - 1, h)
        else:
            want = (x, y, w, kept[3]), (x, y + kept[3] + 1, w, h - kept[3] - 1)
        self.assertEqual((kept, added), want, f"{name}: {side} of {target} ({before[target]})")
        self.assertTrue(min(*kept[2:], *added[2:]) > 0, (kept, added))

    def test_open_close_retiles_on_an_open_only(self):
        width = self.server.size[0] // 2
        self.started("w1")
        self.started("w2")
        self.grid([["w1", "w2"]], width)
        self.open_close_hooks(MANAGER)
        self.assertEqual(
            self.window_option(MANAGER, tmux_host.GRID_PANES_SET), self.pane_set(MANAGER)
        )
        dragged = self.drag("w1", 5)
        self.holds(self.layout, dragged, "the layout after a drag")
        tiles = self.tiles()
        self.tmux("resize-window", "-t", self.window_of(MANAGER), "-x", "160", "-y", "40")
        self.assertEqual(self.display(MANAGER, "#{window_width}x#{window_height}"), "160x40")
        heights = [self.panes()[self.server.pane_of(n)][3] for n in ("w1", "w2")]
        self.assertGreater(heights[0] - heights[1], 1)
        self.holds(
            lambda: (self.layout(), self.tiles()),
            (self.layout(), tiles),
            "the layout and @grid-tiles after a window resize",
        )
        width = self.panes()[self.manager][2]
        self.started("w3")
        self.grid([["w1", "w2", "w3"]], width)
        self.open_close_hooks(MANAGER)
        self.holds(self.layout, self.layout(), "the layout after an open")

    def test_open_close_retiles_on_a_close(self):
        width = self.server.size[0] // 2
        for name in ("w1", "w2", "w3"):
            self.started(name)
        self.grid([["w1", "w2", "w3"]], width)
        # the most recently active client shows w1's session, not mgr's: tmux 3.4 then skips the window's pane-exited
        # hook, so window-layout-changed (a new pane set) re-tiles
        self.server.attach("w1")
        self.driver.close("w2")
        self.grid([["w1", "w3"]], width)
        self.open_close_hooks(MANAGER)
        self.driver.close("w3")
        self.grid([["w1"]], width)
        live_tmux.wait(
            lambda: self.window_option(MANAGER, tmux_host.GRID_PANES_SET) == self.pane_set(MANAGER),
            what="@grid-panes the window's pane set",
        )

    def test_off_places_only_till_tile(self):
        self.retile("off")
        before = self.panes()
        self.started("w1")
        w1 = self.server.pane_of("w1")
        self.split_of(before, "w1", self.manager, "right")
        before = self.panes()
        self.started("w2")
        self.split_of(before, "w2", w1, "below")
        self.drag("w1", -10)
        before = self.panes()
        largest = max(
            (p for p in before if p != self.manager),
            key=lambda p: (before[p][2] * before[p][3], int(p[1:])),
        )
        self.assertEqual(largest, self.server.pane_of("w2"))
        self.started("z1", inside="w1")
        self.split_of(before, "z1", largest, "below")
        self.holds(self.panes, self.panes(), "the panes as placed")
        self.assertEqual(self.hooks(MANAGER), [])
        options = (tmux_host.GRID_MANAGER, tmux_host.GRID_PER_COLUMN)
        self.assertEqual(
            [self.window_option(MANAGER, k) for k in options],
            [self.manager, str(tmux_host.PER_COLUMN)],
        )
        self.tui("tile", self.window_of(MANAGER))
        self.grid([[("w1", ["z1"]), "w2"]], self.server.size[0] // 2)

    def test_a_switch_to_open_close_leaves_only_its_hooks(self):
        self.retile("all")
        self.started("w1")
        self.grid([["w1"]])
        self.assertEqual(self.hooks(MANAGER), list(tmux_host.GRID_HOOKS))
        self.retile("open-close")
        self.started("w2")
        self.grid([["w1", "w2"]])
        self.open_close_hooks(MANAGER)
        dragged = self.drag("w1", 5)
        self.holds(self.layout, dragged, "the layout after a drag")


class SplitsTest(Live):
    def test_split_outside_grid(self):
        manager = self.server.inside(MANAGER)["TMUX_PANE"]
        with self.inside(MANAGER):
            self.assertEqual(self.driver.layout(), MANAGER)
        self.assertIsNone(self.driver.layout("below", MANAGER))
        with self.assertRaises(DriverError) as caught:
            self.driver.layout("below")
        self.assertTrue(
            str(caught.exception).endswith(": run from tmux or iTerm2, or pass split_from"),
            caught.exception,
        )
        self.assertEqual(self.started("w1", inside=None, split="below", split_from=MANAGER), "")
        pane = self.server.pane_of("w1")
        panes = self.server.panes(f"={MANAGER}:")
        self.assertIn(pane, panes)
        (mx, my, _, _), (x, y, _, _) = panes[manager], panes[pane]
        self.assertEqual(x, mx)
        self.assertGreater(y, my)
        self.assertEqual(
            [self.window_option(MANAGER, k) for k in ("@grid-manager", "@grid-per-column")],
            ["", ""],
        )


class StartTest(Live):
    def test_a_command_ending_at_once_gets_its_dead_line_and_state(self):
        events = os.path.join(self.cwd, "events")
        tmux_host.start(
            "s1", ["/bin/sh", "-c", "exit 3"], cwd=self.cwd, env=self.server.env(), events=events
        )

        def dead():
            self.server.tmux("run-shell", "-b", "true")
            self.assertEqual(
                [line.split(" ", 1)[1] for line in live_tmux.events(events)], ["s1 dead"]
            )
            return True

        live_tmux.wait(dead, what="s1's dead line")
        self.assertEqual((tmux_host.status("s1"), self.server.option("s1", "@state")), (3, "dead"))


if __name__ == "__main__":
    unittest.main()
