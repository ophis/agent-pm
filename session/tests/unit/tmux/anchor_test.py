"""Tmux and iTerm anchor discovery from client and environment facts."""

import io
from contextlib import redirect_stderr

from agent_pm.session.hosts.tmux import tmux_host

from session.tests.case import Case
from session.tests.unit.tmux.fixtures import PaneFixture
from session.tests.unit.tmux.support import (
    ATTACH,
    HOSTS,
    INSIDE,
    ITERM,
    OSA_KW,
    OWN,
    PGREP,
    SESSIONS,
    SOCK,
    TMUX_KW,
    Tmux,
    clients_of,
    environ,
    osa,
    panes,
    record,
    split_window,
    which,
)


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
            self.assertIsNone(tmux_host.open_pane("s", split="below", proc=fake))
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
        nested = panes(("0", "/dev/ttys001", "%1"), ("0", "/dev/ttys004", "%5"))
        cases = [
            (ITERM, {}, None, tmux_host.Anchor(("id", ["ABC"]))),
            (
                INSIDE,
                {**own, "list-clients": shown},
                None,
                tmux_host.Anchor(("tty", ["/dev/ttys004"]), "%3", SOCK, "own"),
            ),
            (
                INSIDE,
                {**own, "list-clients": shown, "list-panes": nested},
                None,
                tmux_host.Anchor(None, "%5", SOCK, "own"),
            ),
            (INSIDE, own, None, tmux_host.Anchor(("id", ["ABC"]))),
            (
                {},
                {"list-clients": shown},
                "b",
                tmux_host.Anchor(("tty", ["/dev/ttys004"]), "%9", SOCK, "b"),
            ),
            (
                {},
                {"list-clients": shown, "list-panes": nested},
                "b",
                tmux_host.Anchor(None, "%5", SOCK, "b"),
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
                tmux_host.Anchor(("tty", ["/dev/ttys004"]), "%9", SOCK, "b"),
            ),
            (
                INSIDE,
                {**own, "list-clients": control, "list-panes": nested},
                None,
                tmux_host.Anchor(None, "%3", SOCK, "own"),
            ),
            (
                {},
                {"list-clients": control, "list-panes": nested},
                "b",
                tmux_host.Anchor(None, "%9", SOCK, "b"),
            ),
            (
                {},
                {"list-clients": mixed},
                "b",
                tmux_host.Anchor(("tty", ["/dev/ttys004"]), "%9", SOCK, "b"),
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
            ([live, dead], tmux_host.Anchor(None, "%5", SOCK, "b")),
            ([dead], tmux_host.Anchor(("tty", ["/dev/ttys004"]), "%9", SOCK, "b")),
        ):
            with self.subTest(rows=rows), environ():
                fake = Tmux(results={"list-clients": shown, "list-panes": panes(*rows)})
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
