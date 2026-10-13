"""Retile behaviour."""

from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.model import types as tmux_model

from session.tests import live_tmux
from session.tests.integration.tmux_host.case import MANAGER, Live


class RetileTest(Live):
    def setUp(self):
        super().setUp()
        self.manager = self.server.inside(MANAGER)["TMUX_PANE"]

    def panes(self) -> dict[str, tuple[int, int, int, int]]:
        return self.server.panes(f"={MANAGER}:")

    def layout(self) -> str:
        return self.display(MANAGER, "#{window_layout}")

    def tiles(self) -> str:
        return self.window_option(MANAGER, tmux_model.GRID_TILES)

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
            self.window_option(MANAGER, tmux_model.GRID_PANES_SET), self.pane_set(MANAGER)
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
        # the most recently active client shows w1's session, not mgr's: tmux 3.4 then skips the
        # window's pane-exited hook, so window-layout-changed (a new pane set) re-tiles
        self.server.attach("w1")
        self.driver.close("w2")
        self.grid([["w1", "w3"]], width)
        self.open_close_hooks(MANAGER)
        self.driver.close("w3")
        self.grid([["w1"]], width)
        live_tmux.wait(
            lambda: (
                self.window_option(MANAGER, tmux_model.GRID_PANES_SET) == self.pane_set(MANAGER)
            ),
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
        options = (tmux_model.GRID_MANAGER, tmux_model.GRID_PER_COLUMN)
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
        self.assertEqual(self.hooks(MANAGER), list(tmux_model.GRID_HOOKS))
        self.retile("open-close")
        self.started("w2")
        self.grid([["w1", "w2"]])
        self.open_close_hooks(MANAGER)
        dragged = self.drag("w1", 5)
        self.holds(self.layout, dragged, "the layout after a drag")
