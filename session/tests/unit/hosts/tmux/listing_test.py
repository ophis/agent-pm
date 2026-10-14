"""Listing row validation, shown-client precedence and diagnostic tails."""

import re

from agent_pm.session.hosts.tmux import listing

from session.tests.case import Case
from session.tests.tmux_fixtures import (
    ABSENT,
    ALL_CLIENTS,
    COLUMNS,
    FAILED,
    HOST_PANES,
    LISTING,
    TMUX_KW,
    FakeTmux,
    captured,
    expected_row,
    listed,
    panes,
    tabbed,
)


class ListingTest(Case):
    def listing(self, sessions, clients=(0, ""), panes=(0, ""), capture=(0, ""), match=None):
        fake = FakeTmux(
            results={
                "list-sessions": sessions,
                "list-clients": clients,
                "list-panes": panes,
                "capture-pane": capture,
            }
        )
        return listing.listing(match, proc=fake), fake

    def test_every_field_sorted_by_name_one_call_each(self):
        got, fake = self.listing(
            listed(
                ("w2", "0", "sid-2", "fake", "done", "%7", "mgr"),
                ("w1", "1", "sid-1", "other", "dead", "ABC-1", "w0t0p0:ABC-1"),
                ("mgr", "0"),
            ),
            tabbed(
                ("200", "/dev/ttys4", "w2"), ("100", "/dev/ttys9", "w2"), ("5", "/dev/ttys1", "mgr")
            ),
            panes(("0", "/dev/ttys4", "mgr"), ("0", "/dev/ttys2", "w2")),
            (0, "a\nlast\n\n"),
        )
        self.assertEqual(
            got,
            [
                expected_row("mgr", shown=""),
                expected_row(
                    "w1",
                    sid="sid-1",
                    client="other",
                    state="dead",
                    pane="ABC-1",
                    opener="w0t0p0:ABC-1",
                    dead=True,
                    last_lines="a\nlast",
                ),
                expected_row(
                    "w2",
                    sid="sid-2",
                    client="fake",
                    state="done",
                    pane="%7",
                    opener="mgr",
                    shown="mgr",
                ),
            ],
        )
        self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS, HOST_PANES, captured("w1")])
        self.assertEqual(fake.kwargs, [TMUX_KW] * 4)
        self.assertEqual(listing.LAST_LINES, 2000)

    def test_a_name_failing_NAME_or_a_row_tmux_split_wrongly_is_dropped(self):
        got, fake = self.listing(
            (0, "".join(f"{n}\t0\t\t\t\t\t\t\t\n" for n in ("a b", "", "%1", "x;", "#{x}", "ok")))
        )
        self.assertEqual(got, [expected_row("ok")])
        got, _ = self.listing((0, "w1\t0\ts\tid\t\t\t\t\t\t\nw2\t0\nw3\t0\t\t\t\t\t\t\t\n"))
        self.assertEqual(got, [expected_row("w3")])
        forged = listed(("w4", "0", "", "", "", "", "", "mgr\tx"), ("w5", "0"))
        got, _ = self.listing(forged)
        self.assertEqual(got, [expected_row("w5")])
        got, fake = self.listing(listed(("a b", "0")))
        self.assertEqual((got, fake.calls), ([], [LISTING]))

    def test_a_bad_or_empty_field_is_none(self):
        for key, value, want in (
            ("sid", "sid-1", "sid-1"),
            ("sid", "", None),
            ("sid", "s\x1b]0;x\x07", None),
            ("sid", "s\x7f", None),
            ("client", "fake", "fake"),
            ("client", "", None),
            ("client", "\x1b[2J", None),
            ("state", "done", "done"),
            ("state", "", None),
            ("state", "do\x08ne", None),
            ("pane", "%3", "%3"),
            ("pane", "ABC-12", "ABC-12"),
            ("pane", "", None),
            ("pane", "%", None),
            ("pane", "%3;", None),
            ("pane", "w0t0p0:ABC", None),
            ("opener", "mgr", "mgr"),
            ("opener", "w0t0p0:ABC-1", "w0t0p0:ABC-1"),
            ("opener", "", None),
            ("opener", "a b", None),
            ("opener", "x:", None),
            ("opener", "%3", None),
            ("display", "mgr/right/m1", "mgr/right/m1"),
            ("display", "elsewhere:1.2", "elsewhere:1.2"),
            ("display", "", None),
            ("display", "mgr;", None),
            ("display", "a\x1bb", None),
            ("dead", "0", False),
            ("dead", "1", True),
            ("dead", "", None),
            ("dead", "2", None),
        ):
            with self.subTest(key=key, value=value):
                fields = {**dict.fromkeys(COLUMNS, ""), "name": "s", "dead": "0", key: value}
                got, _ = self.listing(listed(tuple(fields[c] for c in COLUMNS)))
                self.assertEqual(got[0][key], want)

    def test_a_dead_rows_last_lines_live_rows_none(self):
        printed = {
            "=d1:": (0, "x\n  y\n\n"),
            "=d2:": (1, "", "boom\n"),
            "=d3:": (0, "\n \n"),
            "=d4:": FileNotFoundError(2, "No such file", "tmux"),
            "=d5:": (0, "".join(f"{i}\n" for i in range(2001)) + "\n\n"),
        }
        got, fake = self.listing(
            listed(*((n, "1") for n in ("d1", "d2", "d3", "d4", "d5")), ("live", "0"), ("odd", "")),
            capture=lambda argv: printed[argv[argv.index("-t") + 1]],
        )
        self.assertEqual(
            [(r["name"], r["last_lines"]) for r in got],
            [
                ("d1", "x\n  y"),
                ("d2", None),
                ("d3", None),
                ("d4", None),
                ("d5", "\n".join(map(str, range(1, 2001)))),
                ("live", None),
                ("odd", None),
            ],
        )
        self.assertEqual(fake.calls[2:], [captured(n) for n in ("d1", "d2", "d3", "d4", "d5")])

    def test_a_launched_session_reads_its_own_pane_whichever_pane_is_active(self):
        got, fake = self.listing(
            listed(
                ("w1", "0", *[""] * 6, "4242 $1 %7 4343"),
                ("w2", "1", *[""] * 6, "4242 $2 %8 4344"),
                ("w3", "0", *[""] * 6, "4242 $3 %9 4345"),
                ("w4", "1", *[""] * 6, "4242 $4 %x 4346"),
                ("mgr", "1"),
            ),
            panes=panes(
                ("1", "", "w1", "%7"),
                ("0", "/dev/ttys1", "w1", "%10"),
                ("0", "/dev/ttys2", "w2", "%8"),
                ("1", "", "w2", "%11"),
                ("0", "/dev/ttys3", "w3", "%12"),
            ),
            capture=lambda argv: {"%7": (0, "agent\n"), "=mgr:": (0, "manager\n")}[
                argv[argv.index("-t") + 1]
            ],
        )
        self.assertEqual(
            [(r["name"], r["dead"], r["last_lines"]) for r in got],
            [
                ("mgr", True, "manager"),
                ("w1", True, "agent"),
                ("w2", False, None),
                ("w3", True, None),
                ("w4", None, None),
            ],
        )
        self.assertEqual(
            fake.calls, [LISTING, ALL_CLIENTS, HOST_PANES, captured("mgr"), captured("%7")]
        )

    def test_shown_by_the_most_recent_client_in_its_host_session(self):
        names = ("a", "b", "c", "d", "e", "f", "g", "h", "i")
        got, fake = self.listing(
            listed(*((n, "0") for n in names)),
            tabbed(
                ("7", "/dev/host", "b"),
                ("7", "/dev/term", "c"),
                ("7", "/dev/bad", "d"),
                ("7", "/dev/gone", "e"),
                ("10", "/dev/host", "f"),
                ("20", "/dev/term", "f"),
                ("9", "/dev/term", "g"),
                ("10", "/dev/host", "g"),
                ("x", "/dev/host", "h"),
                ("7", "", "h"),
                ("7", "/dev/host", "h", "extra"),
                ("7", "/dev/host"),
                ("7", "/dev/host", "zz"),
            ),
            panes(
                ("0", "/dev/host", "host"),
                ("0", "/dev/bad", "bad name"),
                ("1", "/dev/gone", "host2"),
            ),
        )
        self.assertEqual(
            {r["name"]: r["shown"] for r in got},
            {
                "a": None,
                "b": "host",
                "c": "",
                "d": "",
                "e": "",
                "f": "",
                "g": "host",
                "h": None,
                "i": None,
            },
        )
        self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS, HOST_PANES])

    def test_host_panes_only_when_a_listed_session_has_a_client_or_a_launch(self):
        for clients, match, want in (
            ((0, ""), None, [expected_row("mgr"), expected_row("w1")]),
            (tabbed(("7", "/dev/host", "w1")), re.compile("mgr"), [expected_row("mgr")]),
        ):
            with self.subTest(clients=clients):
                got, fake = self.listing(listed(("mgr", "0"), ("w1", "0")), clients, match=match)
                self.assertEqual(got, want)
                self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS])
        got, fake = self.listing(listed(("w1", "0", *[""] * 6, "4242 $1 %7 4343")))
        self.assertEqual(got, [expected_row("w1", dead=True)])
        self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS, HOST_PANES])

    def test_match_fullmatches_before_any_capture(self):
        sessions = listed(("w1", "1"), ("w10", "1"), ("mgr", "1"), ("xw1", "0"))
        for match, want in (
            (re.compile("w1"), ["w1"]),
            (re.compile("w[0-9]+"), ["w1", "w10"]),
            (None, ["mgr", "w1", "w10", "xw1"]),
        ):
            with self.subTest(match=match):
                got, fake = self.listing(sessions, match=match)
                self.assertEqual([r["name"] for r in got], want)
                self.assertEqual(
                    fake.calls, [LISTING, ALL_CLIENTS, *(captured(n) for n in want if n != "xw1")]
                )
        got, fake = self.listing(sessions, match=re.compile("w"))
        self.assertEqual((got, fake.calls), ([], [LISTING]))

    def test_no_server_is_empty(self):
        for result in (*((1, "", err + "\n") for err in ABSENT), (0, "")):
            with self.subTest(result=result):
                got, fake = self.listing(result)
                self.assertEqual((got, fake.calls), ([], [LISTING]))

    def test_tmux_failing_is_none(self):
        sessions, client = listed(("w1", "0")), tabbed(("7", "/dev/host", "w1"))
        for failing in (
            *((1, "", err + "\n") for err in FAILED),
            FileNotFoundError(2, "No such file", "tmux"),
        ):
            for at, calls in (
                ({"sessions": failing}, [LISTING]),
                ({"clients": failing}, [LISTING, ALL_CLIENTS]),
                ({"clients": client, "panes": failing}, [LISTING, ALL_CLIENTS, HOST_PANES]),
            ):
                with self.subTest(failing=failing, at=at):
                    got, fake = self.listing(**{"sessions": sessions, **at})
                    self.assertEqual((got, fake.calls), (None, calls))
