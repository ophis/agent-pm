"""Tmux listing behavior at standard-library boundaries."""

import re

from agent_pm.session.hosts.tmux import tmux_host

from session.tests.case import Case
from session.tests.unit.tmux.support import (
    ABSENT,
    ALL_CLIENTS,
    COLUMNS,
    FAILED,
    FORMAT,
    HOST_PANES,
    LISTING,
    TMUX_KW,
    Tmux,
    captured,
    listed,
    row,
    tabbed,
)


class ListingTest(Case):
    def listing(self, sessions, clients=(0, ""), panes=(0, ""), capture=(0, ""), match=None):
        fake = Tmux(
            results={
                "list-sessions": sessions,
                "list-clients": clients,
                "list-panes": panes,
                "capture-pane": capture,
            }
        )
        return tmux_host.listing(match, proc=fake), fake

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
            tabbed(("0", "/dev/ttys4", "mgr"), ("0", "/dev/ttys2", "w2")),
            (0, "a\nlast\n\n"),
        )
        self.assertEqual(
            got,
            [
                row("mgr", shown=""),
                row(
                    "w1",
                    sid="sid-1",
                    runtime="other",
                    state="dead",
                    pane="ABC-1",
                    opener="w0t0p0:ABC-1",
                    dead=True,
                    last_lines="a\nlast",
                ),
                row(
                    "w2",
                    sid="sid-2",
                    runtime="fake",
                    state="done",
                    pane="%7",
                    opener="mgr",
                    shown="mgr",
                ),
            ],
        )
        self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS, HOST_PANES, captured("w1")])
        self.assertEqual(fake.kwargs, [TMUX_KW] * 4)
        self.assertEqual(tmux_host.LAST_LINES, 2000)

    def test_a_name_failing_NAME_or_a_row_tmux_split_wrongly_is_dropped(self):
        got, fake = self.listing(
            (0, "".join(f"{n}\t0\t\t\t\t\t\n" for n in ("a b", "", "%1", "x;", "#{x}", "ok")))
        )
        self.assertEqual(got, [row("ok")])
        got, _ = self.listing((0, "w1\t0\ts\tid\t\t\t\t\nw2\t0\nw3\t0\t\t\t\t\t\n"))
        self.assertEqual(got, [row("w3")])
        got, fake = self.listing(listed(("a b", "0")))
        self.assertEqual((got, fake.calls), ([], [LISTING]))

    def test_a_bad_or_empty_field_is_none(self):
        for key, value, want in (
            ("sid", "sid-1", "sid-1"),
            ("sid", "", None),
            ("sid", "s\x1b]0;x\x07", None),
            ("sid", "s\x7f", None),
            ("runtime", "fake", "fake"),
            ("runtime", "", None),
            ("runtime", "\x1b[2J", None),
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
            tabbed(
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

    def test_host_panes_only_when_a_listed_session_has_a_client(self):
        for clients, match, want in (
            ((0, ""), None, [row("mgr"), row("w1")]),
            (tabbed(("7", "/dev/host", "w1")), re.compile("mgr"), [row("mgr")]),
        ):
            with self.subTest(clients=clients):
                got, fake = self.listing(listed(("mgr", "0"), ("w1", "0")), clients, match=match)
                self.assertEqual(got, want)
                self.assertEqual(fake.calls, [LISTING, ALL_CLIENTS])

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


class OptionTest(Case):
    def test_value(self):
        for printed, want in (("0123abcd\n", "0123abcd"), ("a b\n", "a b"), ("\n", "")):
            with self.subTest(printed=printed):
                fake = Tmux(results={"show-options": (0, printed)})
                self.assertEqual(tmux_host.option("s", "@sid", proc=fake), want)
                self.assertEqual(fake.calls, [["tmux", "show-options", "-v", "-t", "=s:", "@sid"]])
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_none_when_unset_or_absent(self):
        for err in ("invalid option: @sid", *ABSENT):
            with self.subTest(err=err):
                fake = Tmux(results={"show-options": (1, "", err + "\n")})
                self.assertIsNone(tmux_host.option("s", "@sid", proc=fake))

    def test_another_failure_raises(self):
        for result in (
            (1, "", FAILED[0] + "\n"),
            (1, "", "boom\n"),
            FileNotFoundError(2, "No such file", "tmux"),
        ):
            with self.subTest(result=result), self.assertRaisesRegex(tmux_host.TuiError, "^tmux: "):
                tmux_host.option("s", "@sid", proc=Tmux(results={"show-options": result}))

    def test_only_a_user_option(self):
        for name in ("status", "@", "@a b", "@a;", "", "-g"):
            with self.subTest(name=name):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, "^option "):
                    tmux_host.option("s", name, proc=fake)
                self.assertEqual(fake.calls, [])


class PaneIdTest(Case):
    def test_its_pane(self):
        fake = Tmux(pane="%5")
        self.assertEqual(tmux_host.pane("s", proc=fake), "%5")
        self.assertEqual(fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", "#{pane_id}"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_none_when_absent(self):
        # tmux (3.7) exits 0 printing nothing for a missing session
        for result in (*((1, "", err + "\n") for err in ABSENT), (0, "\n"), (0, "")):
            with self.subTest(result=result):
                self.assertIsNone(
                    tmux_host.pane("s", proc=Tmux(results={"display-message": result}))
                )

    def test_a_failure_or_junk_raises(self):
        for result, error in (
            ((1, "", "boom\n"), "^tmux: boom$"),
            ((1, "", FAILED[0] + "\n"), "^tmux: "),
            ((0, "%5;\n"), "^bad tmux pane '%5;'$"),
            ((0, "5\n"), "^bad tmux pane '5'$"),
            ((0, "%5\n%6\n"), "^bad tmux pane"),
        ):
            with self.subTest(result=result), self.assertRaisesRegex(tmux_host.TuiError, error):
                tmux_host.pane("s", proc=Tmux(results={"display-message": result}))


class PaneTargetTest(Case):
    """status, kill, paste, keys, option and read take a pane id (%N) as their target."""

    def calls(self, target, fake):
        return [
            lambda: tmux_host.status(target, proc=fake),
            lambda: tmux_host.kill(target, proc=fake),
            lambda: tmux_host.paste(target, "hi", proc=fake),
            lambda: tmux_host.keys(target, "Enter", proc=fake),
            lambda: tmux_host.option(target, "@sid", proc=fake),
            lambda: tmux_host.read(target, proc=fake),
        ]

    def test_a_pane_id_is_the_target_as_is(self):
        fake = Tmux(pane="0  ")
        for call in self.calls("%12", fake):
            call()
        buffer = fake.calls[2][3]
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "display-message", "-p", "-t", "%12", FORMAT],
                ["tmux", "kill-session", "-t", "%12"],
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", "%12"],
                ["tmux", "send-keys", "-t", "%12", "Enter"],
                ["tmux", "show-options", "-v", "-t", "%12", "@sid"],
                ["tmux", "capture-pane", "-p", "-J", "-t", "%12"],
            ],
        )

    def test_a_gone_pane_is_absent(self):
        gone = (1, "", "can't find pane: %12\n")
        self.assertIsNone(tmux_host.status("%12", proc=Tmux(results={"display-message": gone})))
        self.assertIsNone(
            tmux_host.option("%12", "@sid", proc=Tmux(results={"show-options": gone}))
        )
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: can't find pane: %12$"):
            tmux_host.kill("%12", proc=Tmux(results={"kill-session": gone}))

    def test_a_bad_target_is_refused_before_tmux(self):
        for target in ("%", "%x", "%1;", "%1 ", "%1:", "%1.0", "=%1", "%-1", "a b"):
            with self.subTest(target=target):
                fake = Tmux()
                for call in self.calls(target, fake):
                    with self.assertRaisesRegex(tmux_host.TuiError, "^invalid target "):
                        call()
                self.assertEqual(fake.calls, [])
