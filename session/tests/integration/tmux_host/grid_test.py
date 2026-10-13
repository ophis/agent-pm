"""Grid behaviour."""

import os

from agent_pm.session.engine.types import DriverError
from agent_pm.session.hosts.tmux import tmux_host

from session.tests import live_tmux
from session.tests.integration.tmux_host.case import IGNORED, MANAGER, Live


class GridTest(Live):
    def setUp(self):
        super().setUp()
        self.retile("all")

    def test_columns_retile(self):
        self.per_column(2)
        # tmux's split leaves the split pane the wider half (100 of 200); tile keeps the manager's
        # width
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
            "s1",
            tmux_host.Command(
                argv=["/bin/sh", "-c", "exit 3"], cwd=self.cwd, env=self.server.env()
            ),
            options=tmux_host.StartOptions(events=events),
        )

        def dead():
            self.server.tmux("run-shell", "-b", "true")
            self.assertEqual(
                [line.split(" ", 1)[1] for line in live_tmux.events(events)], ["s1 dead"]
            )
            return True

        live_tmux.wait(dead, what="s1's dead line")
        self.assertEqual((tmux_host.status("s1"), self.server.option("s1", "@state")), (3, "dead"))
