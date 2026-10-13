"""Tmux tile application behavior at standard-library boundaries."""

from agent_pm.session.hosts.tmux import tmux_host

from session.tests.unit.tmux.fixtures import TileFixture
from session.tests.unit.tmux.support import (
    SESSIONS,
    dump,
    grid,
    hold,
    slot,
)


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
                fake = self.tile(*self.WIDE, if_panes_changed=True, recorded=recorded)
                self.assertEqual(
                    fake.calls,
                    [self.SOCKET, self.READ, self.PANES, SESSIONS, *self.applied(self.WIDE_SENT)],
                )

    def test_if_panes_changed_reads_only_holding_the_lock(self):
        release = hold(self, self.lock)
        fake = self.tile(*self.WIDE, wait=False, if_panes_changed=True, recorded="")
        self.assertEqual(fake.calls, [self.SOCKET])
        release()
        fake = self.tile(*self.WIDE, wait=False, if_panes_changed=True, recorded="")
        self.assertEqual(
            fake.calls,
            [self.SOCKET, self.READ, self.PANES, SESSIONS, *self.applied(self.WIDE_SENT)],
        )

    def test_if_panes_changed_zoomed_no_grid_or_torn_down_records_nothing(self):
        for kw in ({"zoomed": "1"}, {"manager": ""}):
            with self.subTest(**kw):
                fake = self.tile(*self.WIDE, if_panes_changed=True, recorded="", **kw)
                self.assertEqual(fake.calls, [self.SOCKET, self.READ])
        fake = self.tile(
            dump("160x48,0,0[160x16,0,0,1,160x15,0,17,2,160x15,0,33,3]"),
            ["%1", "%2", "%3"],
            self.W3,
            if_panes_changed=True,
            recorded="%0 %1 %2 %3",
        )
        self.assertEqual(fake.calls, [self.SOCKET, self.READ, self.PANES, self.TEARDOWN])
        fake = self.tile(
            "bogus", ["%0", "%1", "%2", "%3"], self.W3, if_panes_changed=True, recorded=""
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
                "200x50,0,0{99x50,0,0,0,100x50,100,0[100x25,100,0{50x25,100,0,2,49x25,151,0,3},100x24,100,26,1]}",
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
                self.assertEqual(tmux_host._grid_pass("@3", None, False, fake), want)
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
                    "9a41,160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0{40x24,80,0,1,39x24,121,0,3},80x23,80,25,2]}",
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
