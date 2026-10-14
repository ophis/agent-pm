"""The tmux command transport against a recorded tmux: status, process identity, kill, send,
paste, keys, read, options and pane targets.
"""

import os
import tempfile
import unittest
import unittest.mock

from agent_pm.session.hosts.tmux import transport, types

from session.tests.case import Case
from session.tests.tmux_fixtures import (
    ABSENT,
    FAILED,
    FORMAT,
    TMUX_KW,
    FakeTmux,
    environ,
)


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
                self.assertEqual(transport.process(target, proc=fake), want)
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
                    transport.process("s", proc=fake)
        fake = FakeTmux()
        with self.assertRaisesRegex(types.TuiError, "invalid target"):
            transport.process("a b", proc=fake)
        self.assertEqual(fake.calls, [])


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
            ("1  ", types.PaneStatus.RUNNING),
            ("  ", None),
            (None, None),
        ):
            with self.subTest(pane=pane):
                fake = FakeTmux(pane=pane)
                self.assertEqual(transport.status("s", proc=fake), want)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW])

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
                self.assertIs(transport.status("s", proc=fake), want)

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
                self.assertEqual(transport.status("s", proc=fake), want)
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
        transport.kill("s", proc=fake)
        self.assertEqual(fake.calls, [["tmux", "kill-session", "-t", "=s"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_failure(self):
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            transport.kill("s", proc=FakeTmux(fail={"kill-session"}))


class SendTest(Case):
    def send(self, fake, text):
        transport.send(
            "s", text, proc=fake, sleep=lambda seconds: fake.calls.append(("sleep", seconds))
        )
        buffer = fake.calls[0][3]
        self.assertRegex(buffer, r"^tui-s-[0-9a-f]{32}$")
        return buffer

    def test_pastes_the_text_then_presses_enter(self):
        fake = FakeTmux()
        buffer = self.send(fake, "-x hi")
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "=s:"],
                ("sleep", 0.5),
                ["tmux", "send-keys", "-t", "=s:", "Enter"],
            ],
        )
        self.assertEqual(
            fake.kwargs,
            [{"capture_output": True, "text": True, "input": "-x hi"}, TMUX_KW, TMUX_KW],
        )

    def test_text_goes_through_stdin_verbatim(self):
        long_lines = "\n".join(f"line {i:02d} " + "x" * 50 for i in range(28))
        for text in ("ls;", ";", "a\\;", "a;b", long_lines, "one\ntwo;\n"):
            with self.subTest(text=text[:20]):
                fake = FakeTmux()
                self.send(fake, text)
                self.assertEqual(fake.kwargs[0]["input"], text)
                self.assertNotIn(text, [a for call in fake.calls for a in call])

    def test_a_buffer_per_call(self):
        fake = FakeTmux()
        first = self.send(fake, "a")
        fake.calls = []
        self.assertNotEqual(self.send(fake, "a"), first)

    def test_failure(self):
        for step, commands in (
            ("load-buffer", ["load-buffer"]),
            ("paste-buffer", ["load-buffer", "paste-buffer", "delete-buffer"]),
        ):
            with self.subTest(step=step):
                fake, sleep = FakeTmux(fail={step}), unittest.mock.Mock()
                with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
                    transport.send("s", "hi", proc=fake, sleep=sleep)
                self.assertEqual(fake.commands(), commands)
                sleep.assert_not_called()


class PasteTest(Case):
    def test_loads_the_buffer_then_pastes_it_bracketed_deleting_it(self):
        fake = FakeTmux()
        self.assertIsNone(transport.paste("s", "a;\nb", proc=fake))
        buffer = fake.calls[0][3]
        self.assertRegex(buffer, r"^tui-s-[0-9a-f]{32}$")
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "=s:"],
            ],
        )
        self.assertEqual(
            fake.kwargs, [{"capture_output": True, "text": True, "input": "a;\nb"}, TMUX_KW]
        )

    def test_a_failed_paste_deletes_its_buffer(self):
        for delete in ((0,), (1, "", "no buffer\n"), OSError("gone")):
            with self.subTest(delete=delete):
                fake = FakeTmux(
                    results={"paste-buffer": (1, "", "boom\n"), "delete-buffer": delete}
                )
                with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
                    transport.paste("s", "hi", proc=fake)
                buffer = fake.calls[0][3]
                self.assertEqual(
                    fake.calls[1:],
                    [
                        ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "=s:"],
                        ["tmux", "delete-buffer", "-b", buffer],
                    ],
                )
        fake = FakeTmux(fail={"load-buffer"})
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            transport.paste("s", "hi", proc=fake)
        self.assertEqual(fake.commands(), ["load-buffer"])


class KeysTest(Case):
    def test_send_keys_names_the_keys(self):
        fake = FakeTmux()
        self.assertIsNone(transport.keys("s", "Escape", "C-c", "Enter", proc=fake))
        self.assertEqual(fake.calls, [["tmux", "send-keys", "-t", "=s:", "Escape", "C-c", "Enter"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_a_name_tmux_would_misread_is_refused_before_tmux(self):
        for name in ("-l", "-", "Enter;", ";"):
            with self.subTest(name=name):
                fake = FakeTmux()
                with self.assertRaisesRegex(types.TuiError, "^tmux would misread"):
                    transport.keys("s", "Escape", name, proc=fake)
                self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            transport.keys("s", "Enter", proc=FakeTmux(fail={"send-keys"}))


class ReadTest(Case):
    def test_visible_pane_or_last_lines(self):
        for lines, printed, want, extra in (
            (None, "a\n  b\n\n \n", "a\n  b", []),
            (3, "1\n2\n\n3\n4\n\n\n", "\n3\n4", ["-S", "-3"]),
        ):
            with self.subTest(lines=lines):
                fake = FakeTmux(results={"capture-pane": (0, printed)})
                self.assertEqual(transport.read("s", lines, proc=fake), want)
                self.assertEqual(
                    fake.calls, [["tmux", "capture-pane", "-p", "-J", "-t", "=s:", *extra]]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_lines_must_be_a_positive_int(self):
        fake = FakeTmux()
        for lines in (0, -1, True, 1.5, "3"):
            with self.subTest(lines=lines), self.assertRaises(types.TuiError):
                transport.read("s", lines, proc=fake)
        self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(types.TuiError, r"^tmux: boom$"):
            transport.read("s", proc=FakeTmux(fail={"capture-pane"}))


class OptionTest(Case):
    def test_value(self):
        for printed, want in (("0123abcd\n", "0123abcd"), ("a b\n", "a b"), ("\n", "")):
            with self.subTest(printed=printed):
                fake = FakeTmux(results={"show-options": (0, printed)})
                self.assertEqual(transport.option("s", "@sid", proc=fake), want)
                self.assertEqual(fake.calls, [["tmux", "show-options", "-v", "-t", "=s:", "@sid"]])
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_none_when_unset_or_absent(self):
        for err in ("invalid option: @sid", *ABSENT):
            with self.subTest(err=err):
                fake = FakeTmux(results={"show-options": (1, "", err + "\n")})
                self.assertIsNone(transport.option("s", "@sid", proc=fake))

    def test_another_failure_raises(self):
        for result in (
            (1, "", FAILED[0] + "\n"),
            (1, "", "boom\n"),
            FileNotFoundError(2, "No such file", "tmux"),
        ):
            with self.subTest(result=result), self.assertRaisesRegex(types.TuiError, "^tmux: "):
                transport.option("s", "@sid", proc=FakeTmux(results={"show-options": result}))

    def test_only_a_user_option(self):
        for name in ("status", "@", "@a b", "@a;", "", "-g"):
            with self.subTest(name=name):
                fake = FakeTmux()
                with self.assertRaisesRegex(types.TuiError, "^option "):
                    transport.option("s", name, proc=fake)
                self.assertEqual(fake.calls, [])


class PaneIdTest(Case):
    def test_its_pane(self):
        fake = FakeTmux(pane="%5")
        self.assertEqual(transport.pane("s", proc=fake), "%5")
        self.assertEqual(fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", "#{pane_id}"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_none_when_absent(self):
        # tmux (3.7) exits 0 printing nothing for a missing session
        for result in (*((1, "", err + "\n") for err in ABSENT), (0, "\n"), (0, "")):
            with self.subTest(result=result):
                self.assertIsNone(
                    transport.pane("s", proc=FakeTmux(results={"display-message": result}))
                )

    def test_a_failure_or_junk_raises(self):
        for result, error in (
            ((1, "", "boom\n"), "^tmux: boom$"),
            ((1, "", FAILED[0] + "\n"), "^tmux: "),
            ((0, "%5;\n"), "^bad tmux pane '%5;'$"),
            ((0, "5\n"), "^bad tmux pane '5'$"),
            ((0, "%5\n%6\n"), "^bad tmux pane"),
        ):
            with self.subTest(result=result), self.assertRaisesRegex(types.TuiError, error):
                transport.pane("s", proc=FakeTmux(results={"display-message": result}))


class PaneTargetTest(Case):
    """status, kill, paste, keys, option and read take a pane id (%N) as their target."""

    def calls(self, target, fake):
        return [
            lambda: transport.status(target, proc=fake),
            lambda: transport.kill(target, proc=fake),
            lambda: transport.paste(target, "hi", proc=fake),
            lambda: transport.keys(target, "Enter", proc=fake),
            lambda: transport.option(target, "@sid", proc=fake),
            lambda: transport.read(target, proc=fake),
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
                ["tmux", "kill-session", "-t", "%12"],
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", "%12"],
                ["tmux", "send-keys", "-t", "%12", "Enter"],
                ["tmux", "show-options", "-v", "-t", "%12", "@sid"],
                ["tmux", "capture-pane", "-p", "-J", "-t", "%12"],
            ],
        )

    def test_a_gone_pane_is_absent(self):
        gone = (1, "", "can't find pane: %12\n")
        self.assertIsNone(transport.status("%12", proc=FakeTmux(results={"display-message": gone})))
        self.assertIsNone(
            transport.option("%12", "@sid", proc=FakeTmux(results={"show-options": gone}))
        )
        with self.assertRaisesRegex(types.TuiError, r"^tmux: can't find pane: %12$"):
            transport.kill("%12", proc=FakeTmux(results={"kill-session": gone}))

    def test_a_bad_target_is_refused_before_tmux(self):
        for target in ("%", "%x", "%1;", "%1 ", "%1:", "%1.0", "=%1", "%-1", "a b"):
            with self.subTest(target=target):
                fake = FakeTmux()
                for call in self.calls(target, fake):
                    with self.assertRaisesRegex(types.TuiError, "^invalid target "):
                        call()
                self.assertEqual(fake.calls, [])
