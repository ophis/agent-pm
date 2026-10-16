"""Tmux's tmux verbs against a recorded tmux: status, process identity, kill, send, paste, keys,
read, options and pane targets; listing row validation, shown-client precedence and diagnostic
tails.
"""

import os
import re
import tempfile

from agent_pm.session.hosts.tmux import transport, types

from session.tests.case import Case
from session.tests.tmux_fixtures import (
    ABSENT,
    ALL_CLIENTS,
    COLUMNS,
    FAILED,
    FORMAT,
    HOST_PANES,
    LISTING,
    TMUX_KW,
    FakeTmux,
    captured,
    environ,
    expected_row,
    listed,
    panes,
    tabbed,
)


def out_of_mode(target):
    """The copy-mode -q a paste or keys call chains first."""
    return ["tmux", "copy-mode", "-q", "-t", target, ";"]


class DisplayErrorTest(Case):
    def test_none_or_printable_text_not_ending_in_a_semicolon(self):
        for value in (None, "", "mgr", "mgr/right/m1", "elsewhere:1.2", "a b#{x}", "\u00e9"):
            with self.subTest(value=value):
                self.assertIsNone(transport.display_error(value))
        for value in ("a\tb", "a\nb", "a\0b", "a\u2028b", "mgr;", 5, b"x"):
            with self.subTest(value=value):
                self.assertEqual(
                    transport.display_error(value),
                    f"display {value!r:.80}: want printable text not ending in ;",
                )


class ProcessIdentityTest(Case):
    FORMAT = "#{pid} #{session_id} #{pane_id} #{pane_pid}"

    def test_the_server_session_pane_and_process_ids_of_its_pane_else_none(self):
        for target, printed, want in (
            ("s", (0, "4242 $3 %7 4343\n"), "4242 $3 %7 4343"),
            ("%7", (0, "4242 $3 %7 4343\n"), "4242 $3 %7 4343"),
            ("s", (0, " \n"), None),
            ("%7", (0, "4242   \n"), None),
            ("%7", (0, "4242  %7 4343\n"), None),
            ("s", (1, "", "can't find session: =s\n"), None),
            ("%7", (1, "", "can't find pane: %7\n"), None),
            ("s", (1, "", "no server running on /tmp/tmux-501/default\n"), None),
        ):
            with self.subTest(target=target, printed=printed):
                fake = FakeTmux(results={"display-message": printed})
                self.assertEqual(transport.Tmux(fake).process(target), want)
                at = "=s:" if target == "s" else target
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", at, self.FORMAT]]
                )

    def test_a_failure_bad_output_or_bad_target_raises(self):
        for printed, error in (
            ((1, "", "boom\n"), "^tmux: boom$"),
            ((0, "4242 $3 %7\n"), "^bad tmux process "),
            ((0, "4242  %7 bad\n"), "^bad tmux process "),
            ((0, "bad  %7 4343\n"), "^bad tmux process "),
        ):
            with self.subTest(printed=printed):
                fake = FakeTmux(results={"display-message": printed})
                with self.assertRaisesRegex(types.TuiError, error):
                    transport.Tmux(fake).process("s")
        fake = FakeTmux()
        with self.assertRaisesRegex(types.TuiError, "invalid target"):
            transport.Tmux(fake).process("a b")
        self.assertEqual(fake.calls, [])


class ProcessStatusTest(Case):
    """The pane's process identity and its status in one display-message."""

    QUERY = (
        "#{pid} #{session_id} #{pane_id} #{pane_pid}\t"
        "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"
    )

    def test_one_call_gives_the_process_and_the_status(self):
        for printed, want in (
            ("4242 $3 %7 4343\t0  ", ("4242 $3 %7 4343", types.PaneStatus.RUNNING)),
            ("4242 $3 %7 4343\t1 3 ", ("4242 $3 %7 4343", 3)),
            ("4242 $3 %7 4343\t1  kill", ("4242 $3 %7 4343", -9)),
            ("4242 $3 %7 4343\tx", ("4242 $3 %7 4343", types.PaneStatus.UNKNOWN)),
            ("4242  %7 4343\t1 0 ", (None, None)),
            ("\t  ", (None, None)),
            (None, (None, None)),
        ):
            with self.subTest(printed=printed):
                fake = FakeTmux(pane=printed)
                self.assertEqual(transport.Tmux(fake).process_status("%7"), want)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "%7", self.QUERY]]
                )

    def test_a_dead_pane_tmux_has_not_reaped_is_running_and_gets_one_no_op_job(self):
        fake = FakeTmux(pane="4242 $3 %7 4343\t1  ")
        self.assertEqual(
            transport.Tmux(fake).process_status("%7"),
            ("4242 $3 %7 4343", types.PaneStatus.RUNNING),
        )
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "display-message", "-p", "-t", "%7", self.QUERY],
                ["tmux", "run-shell", "-b", "true"],
            ],
        )

    def test_a_failing_tmux_or_a_bad_identity_raises(self):
        for fake, message in (
            (FakeTmux(pane="", fail=("display-message",)), "tmux: boom"),
            (FakeTmux(pane="4242 x\t0  "), "bad tmux process '4242 x'"),
        ):
            with self.subTest(message=message), self.assertRaises(types.TuiError) as caught:
                transport.Tmux(fake).process_status("%7")
            self.assertEqual(str(caught.exception), message)


class StatusTest(Case):
    def test_states(self):
        # a signal death: -n (tmux prints the signal's name where libc has one, else its number)
        for pane, want in (
            ("0  ", types.PaneStatus.RUNNING),
            ("1 3 ", 3),
            ("1 0 ", 0),
            ("1  kill", -9),
            ("1  9", -9),
            ("1  term", -15),
            ("  ", None),
            (None, None),
        ):
            with self.subTest(pane=pane):
                fake = FakeTmux(pane=pane)
                self.assertEqual(transport.Tmux(fake).status("s"), want)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_a_dead_pane_tmux_has_not_reaped_is_running_and_gets_one_no_op_job(self):
        for fake in (
            FakeTmux(pane="1  "),
            FakeTmux(pane="1  ", fail=("run-shell",)),
            FakeTmux(pane="1  ", results={"run-shell": OSError("gone")}),
        ):
            with self.subTest(results=fake.results, fail=fake.fail):
                self.assertEqual(transport.Tmux(fake).status("s"), types.PaneStatus.RUNNING)
                self.assertEqual(
                    fake.calls,
                    [
                        ["tmux", "display-message", "-p", "-t", "=s:", FORMAT],
                        ["tmux", "run-shell", "-b", "true"],
                    ],
                )

    def test_a_pane_status_is_a_str_enum_member(self):
        self.assertEqual(list(types.PaneStatus), ["running", "unknown"])
        for name in ("RUNNING", "UNKNOWN"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(transport, name))
        for result, want in (
            ((0, "0  \n"), types.PaneStatus.RUNNING),
            ((0, "x\n"), types.PaneStatus.UNKNOWN),
        ):
            with self.subTest(result=result):
                fake = FakeTmux(results={"display-message": result})
                self.assertIs(transport.Tmux(fake).status("s"), want)

    def test_absent_for_a_missing_session_or_server_else_unknown(self):
        missing = FileNotFoundError(2, "No such file or directory", "tmux")
        for result, want in (
            *(((1, "", err + "\n"), None) for err in ABSENT),
            *(((1, "", err + "\n"), types.PaneStatus.UNKNOWN) for err in FAILED),
            ((0, "x\n"), types.PaneStatus.UNKNOWN),
            ((0, "2 0 \n"), types.PaneStatus.UNKNOWN),
            ((0, "1 0\n"), types.PaneStatus.UNKNOWN),
            ((0, ""), types.PaneStatus.UNKNOWN),
            (missing, types.PaneStatus.UNKNOWN),
            (PermissionError(13, "Permission denied"), types.PaneStatus.UNKNOWN),
        ):
            with self.subTest(result=result):
                fake = FakeTmux(results={"display-message": result})
                self.assertEqual(transport.Tmux(fake).status("s"), want)
                self.assertEqual(len(fake.calls), 1)


class TmuxPathTest(Case):
    def test_the_real_path_of_the_tmux_on_path_else_none(self):
        with tempfile.TemporaryDirectory() as d:
            real, bin_dir = os.path.join(d, "real"), os.path.join(d, "bin")
            os.mkdir(real)
            os.mkdir(bin_dir)
            exe = os.path.join(real, "tmux-3.7")
            with open(exe, "w"):
                pass
            os.chmod(exe, 0o755)
            os.symlink(exe, os.path.join(bin_dir, "tmux"))
            with environ(PATH=bin_dir):
                self.assertEqual(
                    transport.tmux_path(), os.path.join(os.path.realpath(d), "real", "tmux-3.7")
                )
            for path in (real, ""):
                with self.subTest(path=path), environ(PATH=path):
                    self.assertIsNone(transport.tmux_path())


class KillTest(Case):
    def test_kill(self):
        fake = FakeTmux()
        transport.Tmux(fake).kill("s")
        self.assertEqual(fake.calls, [["tmux", "kill-session", "-t", "=s"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_failure(self):
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            transport.Tmux(FakeTmux(fail={"kill-session"})).kill("s")

    def test_a_pane_id_kills_that_pane_only(self):
        fake = FakeTmux()
        transport.Tmux(fake).kill("%12")
        self.assertEqual(fake.calls, [["tmux", "kill-pane", "-t", "%12"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])
        with self.assertRaisesRegex(types.TuiError, r"^tmux: can't find pane: %12$"):
            transport.Tmux(FakeTmux(results={"kill-pane": (1, "", "can't find pane: %12\n")})).kill(
                "%12"
            )


class PasteTest(Case):
    def test_loads_the_buffer_then_pastes_it_bracketed_deleting_it(self):
        fake = FakeTmux()
        self.assertIsNone(transport.Tmux(fake).paste("s", "a;\nb"))
        buffer = fake.calls[0][3]
        self.assertRegex(buffer, r"^tui-s-[0-9a-f]{32}$")
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "load-buffer", "-b", buffer, "-"],
                [*out_of_mode("=s:"), "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "=s:"],
            ],
        )
        self.assertEqual(
            fake.kwargs, [{"capture_output": True, "text": True, "input": "a;\nb"}, TMUX_KW]
        )

    def test_a_failed_paste_deletes_its_buffer(self):
        for delete in ((0,), (1, "", "no buffer\n"), OSError("gone")):
            with self.subTest(delete=delete):
                fake = FakeTmux(results={"copy-mode": (1, "", "boom\n"), "delete-buffer": delete})
                with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
                    transport.Tmux(fake).paste("s", "hi")
                buffer = fake.calls[0][3]
                pasted = ["paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "=s:"]
                self.assertEqual(
                    fake.calls[1:],
                    [[*out_of_mode("=s:"), *pasted], ["tmux", "delete-buffer", "-b", buffer]],
                )
        fake = FakeTmux(fail={"load-buffer"})
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            transport.Tmux(fake).paste("s", "hi")
        self.assertEqual(fake.commands(), ["load-buffer"])

    def test_text_goes_through_stdin_verbatim(self):
        long_lines = "\n".join(f"line {i:02d} " + "x" * 50 for i in range(28))
        for text in ("ls;", ";", "a\\;", "a;b", long_lines, "one\ntwo;\n"):
            with self.subTest(text=text[:20]):
                fake = FakeTmux()
                transport.Tmux(fake).paste("s", text)
                self.assertEqual(fake.kwargs[0]["input"], text)
                buffer = fake.calls[0][3]
                pasted = ["paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "=s:"]
                self.assertEqual(fake.calls[1], [*out_of_mode("=s:"), *pasted])

    def test_a_buffer_per_call(self):
        fake = FakeTmux()
        transport.Tmux(fake).paste("s", "a")
        transport.Tmux(fake).paste("s", "a")
        self.assertNotEqual(fake.calls[0][3], fake.calls[2][3])


class KeysTest(Case):
    def test_send_keys_names_the_keys(self):
        fake = FakeTmux()
        self.assertIsNone(transport.Tmux(fake).keys("s", "Escape", "C-c", "Enter"))
        self.assertEqual(
            fake.calls, [[*out_of_mode("=s:"), "send-keys", "-t", "=s:", "Escape", "C-c", "Enter"]]
        )
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_a_name_tmux_would_misread_is_refused_before_tmux(self):
        for name in ("-l", "-", "Enter;", ";"):
            with self.subTest(name=name):
                fake = FakeTmux()
                with self.assertRaisesRegex(types.TuiError, "^tmux would misread"):
                    transport.Tmux(fake).keys("s", "Escape", name)
                self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            transport.Tmux(FakeTmux(fail={"copy-mode"})).keys("s", "Enter")

    def test_set_option_sets_a_user_option_of_the_targets_session(self):
        fake = FakeTmux()
        self.assertIsNone(transport.Tmux(fake).set_option("%7", "@record", '{"v":1}'))
        self.assertEqual(fake.calls, [["tmux", "set-option", "-t", "%7", "@record", '{"v":1}']])
        for name, value in (("record", "x"), ("@record", "x;"), ("@record", "a\nb")):
            with self.subTest(name=name, value=value), self.assertRaises(types.TuiError):
                transport.Tmux(fake).set_option("%7", name, value)
        self.assertEqual(len(fake.calls), 1)

    def test_press_names_the_keys_after_a_double_dash(self):
        fake = FakeTmux()
        self.assertIsNone(transport.Tmux(fake).press("s", "-", "\\;", "C-c"))
        self.assertEqual(
            fake.calls, [[*out_of_mode("=s:"), "send-keys", "-t", "=s:", "--", "-", "\\;", "C-c"]]
        )

    def test_press_refuses_an_empty_name_or_an_unescaped_semicolon(self):
        for name in ("", ";", "C-;"):
            with self.subTest(name=name):
                fake = FakeTmux()
                with self.assertRaisesRegex(types.TuiError, "^tmux would misread"):
                    transport.Tmux(fake).press("s", "Escape", name)
                self.assertEqual(fake.calls, [])


class ReadTest(Case):
    def test_visible_pane_or_last_lines(self):
        for lines, printed, want, extra in (
            (None, "a\n  b\n\n \n", "a\n  b", []),
            (3, "1\n2\n\n3\n4\n\n\n", "\n3\n4", ["-S", "-3"]),
        ):
            with self.subTest(lines=lines):
                fake = FakeTmux(results={"capture-pane": (0, printed)})
                self.assertEqual(transport.Tmux(fake).read("s", lines), want)
                self.assertEqual(
                    fake.calls, [["tmux", "capture-pane", "-p", "-J", "-t", "=s:", *extra]]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_lines_must_be_a_positive_int(self):
        fake = FakeTmux()
        for lines in (0, -1, True, 1.5, "3"):
            with self.subTest(lines=lines), self.assertRaises(types.TuiError):
                transport.Tmux(fake).read("s", lines)
        self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            transport.Tmux(FakeTmux(fail={"capture-pane"})).read("s")


class OptionTest(Case):
    def test_value(self):
        for printed, want in (("0123abcd\n", "0123abcd"), ("a b\n", "a b"), ("\n", "")):
            with self.subTest(printed=printed):
                fake = FakeTmux(results={"show-options": (0, printed)})
                self.assertEqual(transport.Tmux(fake).option("s", "@sid"), want)
                self.assertEqual(fake.calls, [["tmux", "show-options", "-v", "-t", "=s:", "@sid"]])
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_none_when_unset_or_absent(self):
        for err in ("invalid option: @sid", *ABSENT):
            with self.subTest(err=err):
                fake = FakeTmux(results={"show-options": (1, "", err + "\n")})
                self.assertIsNone(transport.Tmux(fake).option("s", "@sid"))

    def test_another_failure_raises(self):
        for result in (
            (1, "", FAILED[0] + "\n"),
            (1, "", "boom\n"),
            FileNotFoundError(2, "No such file", "tmux"),
        ):
            with self.subTest(result=result), self.assertRaisesRegex(types.TuiError, "^tmux: "):
                transport.Tmux(FakeTmux(results={"show-options": result})).option("s", "@sid")

    def test_only_a_user_option(self):
        for name in ("status", "@", "@a b", "@a;", "", "-g"):
            with self.subTest(name=name):
                fake = FakeTmux()
                with self.assertRaisesRegex(types.TuiError, "^option "):
                    transport.Tmux(fake).option("s", name)
                self.assertEqual(fake.calls, [])


class PaneTargetTest(Case):
    """status, kill, paste, keys, option and read take a pane id (%N) as their target."""

    def calls(self, target, fake):
        return [
            lambda: transport.Tmux(fake).status(target),
            lambda: transport.Tmux(fake).kill(target),
            lambda: transport.Tmux(fake).paste(target, "hi"),
            lambda: transport.Tmux(fake).keys(target, "Enter"),
            lambda: transport.Tmux(fake).option(target, "@sid"),
            lambda: transport.Tmux(fake).read(target),
        ]

    def test_a_pane_id_is_the_target_as_is(self):
        fake = FakeTmux(pane="0  ")
        for call in self.calls("%12", fake):
            call()
        buffer = fake.calls[2][3]
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "display-message", "-p", "-t", "%12", FORMAT],
                ["tmux", "kill-pane", "-t", "%12"],
                ["tmux", "load-buffer", "-b", buffer, "-"],
                [*out_of_mode("%12"), "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "%12"],
                [*out_of_mode("%12"), "send-keys", "-t", "%12", "Enter"],
                ["tmux", "show-options", "-v", "-t", "%12", "@sid"],
                ["tmux", "capture-pane", "-p", "-J", "-t", "%12"],
            ],
        )

    def test_a_gone_pane_is_absent(self):
        gone = (1, "", "can't find pane: %12\n")
        self.assertIsNone(transport.Tmux(FakeTmux(results={"display-message": gone})).status("%12"))
        self.assertIsNone(
            transport.Tmux(FakeTmux(results={"show-options": gone})).option("%12", "@sid")
        )

    def test_a_bad_target_is_refused_before_tmux(self):
        for target in ("%", "%x", "%1;", "%1 ", "%1:", "%1.0", "=%1", "%-1", "a b"):
            with self.subTest(target=target):
                fake = FakeTmux()
                for call in self.calls(target, fake):
                    with self.assertRaisesRegex(types.TuiError, "^invalid target "):
                        call()
                self.assertEqual(fake.calls, [])


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
        return transport.Tmux(fake).listing(match), fake

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
        self.assertEqual(transport.LAST_LINES, 2000)

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
            ("opener", "cmd", "cmd"),
            ("opener", "a_b-1", "a_b-1"),
            ("opener", "w0t0p0:ABC", "w0t0p0:ABC"),
            ("opener", "w0t1p0:6D7E-8F", "w0t1p0:6D7E-8F"),
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
