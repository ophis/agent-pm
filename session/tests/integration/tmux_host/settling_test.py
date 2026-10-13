"""Settling behaviour."""

import subprocess
import sys
import time

from agent_pm.session.hosts.tmux import grids, layout, tmux_host
from agent_pm.session.hosts.tmux.model import types as tmux_model

from session.tests import live_tmux
from session.tests.integration.tmux_host.case import (
    BREAKER_TIMEOUT,
    MANAGER,
    PACE,
    RETILE_WAIT,
    TIMEOUT,
    TUI,
    Live,
)


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
        old = layout.render_layout(
            layout.parse_layout(
                f"200x50,0,0[200x25,0,0{{100x25,0,0,{w2[1:]},"
                f"99x25,101,0,{w1[1:]}}},200x24,0,26,{self.manager[1:]}]"
            )
        )
        self.tmux("select-layout", "-t", self.window, old)
        self.assertEqual(self.layout(), old)

    def layout(self) -> str:
        return self.tmux("display-message", "-p", "-t", self.window, "#{window_layout}")

    def grid_up(self) -> None:
        with self.inside(MANAGER):
            self.assertIsNone(
                grids.open_grid(
                    self.window,
                    self.manager,
                    tmux_host.GridSettings(per_column=1, retile="all"),
                    tmux_host.PaneServices(proc=subprocess.run, script=tmux_host.__file__),
                )
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
        state = (
            f"#{{"
            f"{tmux_model.GRID_MANAGER}"
            f"}}\t#{{"
            f"{tmux_model.GRID_BUSY}"
            f"}}\t#{{"
            f"{tmux_model.GRID_TILES}"
            f"}}"
        )
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
            tmux_model.GRID_MANAGER,
            self.manager,
            ";",
            "set-option",
            "-w",
            "-t",
            self.window,
            tmux_model.GRID_PER_COLUMN,
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
        self.tmux("set-option", "-w", "-t", self.window, tmux_model.GRID_BUSY, self.window)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.assertNotEqual(self.steady(self.layout()), grid)
        self.tmux("set-option", "-u", "-w", "-t", self.window, tmux_model.GRID_BUSY)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.settled()

    def test_breaker_stops_a_loop(self):
        self.grid_up()
        self.settled()
        start, seen, most = time.monotonic(), 0, 0
        tiles = self.window_option(MANAGER, tmux_model.GRID_TILES)
        while True:
            manager, now = self.hand_layout(tiles)
            if not manager:
                break
            seen, most, tiles = seen + (now != tiles), max(most, len(now.split())), now
            elapsed = time.monotonic() - start
            self.assertLess(
                elapsed,
                BREAKER_TIMEOUT,
                (
                    f"still a grid after "
                    f"{elapsed:.0f}"
                    f" s: "
                    f"{seen}"
                    f" re-tiles seen, "
                    f"{seen / elapsed:.1f}"
                    f"/s; it trips past "
                    f"{tmux_model.TILE_LIMIT}"
                    f" in "
                    f"{tmux_model.TILE_WINDOW}"
                    f" s"
                ),
            )
        # the trip follows TILE_LIMIT re-tiles; the loop's last look may miss the last one
        self.assertGreaterEqual(most, tmux_model.TILE_LIMIT - 1)
        options = (
            tmux_model.GRID_MANAGER,
            tmux_model.GRID_PER_COLUMN,
            tmux_model.GRID_TILES,
            tmux_model.GRID_BUSY,
            tmux_model.GRID_PANES_SET,
        )
        self.assertEqual([self.window_option(MANAGER, k) for k in options], [""] * len(options))
        hooks = self.tmux("show-hooks", "-w", "-t", self.window)
        self.assertEqual([h for h in tmux_model.GRID_HOOKS if h in hooks], [], hooks)
        self.tmux("select-layout", "-t", self.window, "even-vertical")
        self.steady(self.layout())
        self.started("w3")
        self.grid([[["w1"], ["w2"], "w3"]])
