"""Tmux layout behavior at standard-library boundaries."""

import io
import itertools
from contextlib import redirect_stderr

from agent_pm.session.hosts.tmux import tmux_host

from session.tests.case import Case
from session.tests.unit.tmux.support import (
    ATTACH,
    HOSTS,
    LIVE,
    MAIN_VERTICAL,
    PGREP,
    SESSIONS,
    TILED_SPLIT,
    Tmux,
    body,
    clients_of,
    environ,
    grid,
    panes,
    record,
    slot,
    split_window,
    which,
)


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
            (root.kind, root.w, root.h, root.x, root.y), (tmux_host.LEFT_RIGHT, width, height, 0, 0)
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
                "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0[40x16,80,0,1,40x15,80,17,2,40x15,80,33,3],"
                "39x48,121,0,4}}",
            ),
            (
                3,
                7,
                "160x48,0,0{79x48,0,0,0,80x48,80,0{26x48,80,0[26x16,80,0,1,26x15,80,17,2,26x15,80,33,3],"
                "26x48,107,0[26x16,107,0,4,26x15,107,17,5,26x15,107,33,6],26x48,134,0,7}}",
            ),
            (
                2,
                3,
                "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0[40x24,80,0,1,40x23,80,25,2],39x48,121,0,3}}",
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
                "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17{40x15,80,17,2,"
                "39x15,121,17[39x7,121,17{19x7,121,17,3,19x7,141,17,6},39x7,121,25,4]},80x15,80,33,5]}",
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
            return panes(("0", "/dev/pts/0", "%0"), ("0", "/dev/pts/2", "%5"))(argv)

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
            return tmux_host.show("s", proc=fake)

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
