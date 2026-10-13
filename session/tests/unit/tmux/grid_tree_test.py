"""Tmux grid tree behavior at standard-library boundaries."""

from agent_pm.session.hosts.tmux import tmux_host

from session.tests.case import Case
from session.tests.unit.tmux.support import (
    grid,
    slot,
)


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
                "160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0{40x24,80,0,1,39x24,121,0[39x12,121,0,3,39x11,121,13,4]},"
                "80x23,80,25,2]}",
                [slot("%1"), slot(None, slot("%3"), slot("%4")), slot("%2")],
            ),
            (
                [*cycle, (6, "x", "a", "%5")],
                ["%0", "%1", "%2", "%3", "%4", "%5"],
                "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0{40x16,80,0,5,39x16,121,0[39x8,121,0,3,39x7,121,9,4]},"
                "80x15,80,17,1,80x15,80,33,2]}",
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
                "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17[80x7,80,17,4,80x7,80,25,5],80x15,80,33,3]}",
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
                "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17,3,80x15,80,33[80x7,80,33,4,80x7,80,41,5]]}",
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
                "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0,1,39x48,121,0[39x24,121,0[39x12,121,0,4,"
                "39x11,121,13,5],39x23,121,25,3]}}",
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
                "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17{40x15,80,17,9,39x15,121,17[39x7,121,17,4,"
                "39x7,121,25,5]},80x15,80,33,3]}",
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
