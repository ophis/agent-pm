"""Tmux stack behavior at standard-library boundaries."""

import io
from contextlib import redirect_stderr

from agent_pm.session.hosts.tmux import tmux_host

from session.tests.case import Case
from session.tests.unit.tmux.support import (
    ATTACH,
    HOSTS,
    INSIDE,
    ITERM,
    LIVE,
    OWN,
    PGREP,
    SESSIONS,
    SOCK,
    WATCH,
    Tmux,
    clients_of,
    environ,
    grid_read,
    osa,
    panes,
    record,
    seq,
    split_window,
    which,
)


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
                else panes(*hosts)(argv)
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
            return tmux_host.show("s", proc=fake, **kw)

    def row(self, number, name, pane, opener="cmd"):
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
        three = [self.row(9, "a", "%9"), self.row(11, "c", "%11"), self.row(10, "b", "%10")]
        right = [clients_of("cmd"), HOSTS, PGREP, split_window("-h", "%3")]
        for rows, live, then in (
            ([self.row(1, "a", "%7")], ["%3", "%7", "%9"], [split_window("-v", "%7")]),
            (three, ["%9", "%10", "%11"], [split_window("-v", "%11")]),
            (three, ["%9", "%10"], [split_window("-v", "%10")]),
            (three, ["%9"], [split_window("-v", "%9")]),
            ([self.row(1, "a", "%7")], ["%3", "%9"], right),
        ):
            with self.subTest(rows=rows, live=live):
                fake = self.fake(rows, live)
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.calls, [OWN, SESSIONS, LIVE, *then, *record("cmd", "%99")])

    def test_tmux_liveness_is_an_exact_line(self):
        fake = self.fake([self.row(1, "a", "%1")], live=["%10", " %1", "%1 ", "x%1", "%1\r"])
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
            self.row(1, "a", "%7"),
            self.row(2, "W", "%8"),
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
        fake = self.fake([*stack, self.row(3, "w2", "%20", "W")], [*live, "%20"], own="W")
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
                fake = self.fake([self.row(1, "a", "%7")], live=["%3", "%7"])
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
            self.row(4, "d", "ID4", opener),
            self.row(3, "c", "%5", opener),
            self.row(2, "b", "ID2", opener),
            self.row(1, "a", "ID1", opener),
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
        fake = self.fake([*bad, self.row(1, "m", "%2")], live)
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
            [self.row(2, "b", "%8"), self.row(1, "a", "%7", "outer")], ["%7", "%8"], own="drv"
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
