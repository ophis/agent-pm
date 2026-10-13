"""Tmux input behavior at standard-library boundaries."""

import io
import subprocess
import unittest
import unittest.mock
from contextlib import redirect_stderr

from agent_pm.session.hosts.tmux import tmux_host

from session.tests.case import Case
from session.tests.unit.tmux.support import (
    ATTACH,
    PREFIXES,
    SH_KW,
    TMUX_KW,
    Tmux,
    environ,
    which,
)


class SendTest(Case):
    def send(self, fake, text):
        tmux_host.send(
            "s", text, proc=fake, sleep=lambda seconds: fake.calls.append(("sleep", seconds))
        )
        buffer = fake.calls[0][3]
        self.assertRegex(buffer, r"^tui-s-[0-9a-f]{32}$")
        return buffer

    def test_pastes_the_text_then_presses_enter(self):
        fake = Tmux()
        buffer = self.send(fake, "-x hi")
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", "=s:"],
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
                fake = Tmux()
                self.send(fake, text)
                self.assertEqual(fake.kwargs[0]["input"], text)
                self.assertNotIn(text, [a for call in fake.calls for a in call])

    def test_a_buffer_per_call(self):
        fake = Tmux()
        first = self.send(fake, "a")
        fake.calls = []
        self.assertNotEqual(self.send(fake, "a"), first)

    def test_failure(self):
        for step, commands in (
            ("load-buffer", ["load-buffer"]),
            ("paste-buffer", ["load-buffer", "paste-buffer", "delete-buffer"]),
        ):
            with self.subTest(step=step):
                fake, sleep = Tmux(fail={step}), unittest.mock.Mock()
                with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
                    tmux_host.send("s", "hi", proc=fake, sleep=sleep)
                self.assertEqual(fake.commands(), commands)
                sleep.assert_not_called()


class PasteTest(Case):
    def test_loads_the_buffer_then_pastes_it_bracketed_deleting_it(self):
        fake = Tmux()
        self.assertIsNone(tmux_host.paste("s", "a;\nb", proc=fake))
        buffer = fake.calls[0][3]
        self.assertRegex(buffer, r"^tui-s-[0-9a-f]{32}$")
        self.assertEqual(
            fake.calls,
            [
                ["tmux", "load-buffer", "-b", buffer, "-"],
                ["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", "=s:"],
            ],
        )
        self.assertEqual(
            fake.kwargs, [{"capture_output": True, "text": True, "input": "a;\nb"}, TMUX_KW]
        )

    def test_a_failed_paste_deletes_its_buffer(self):
        for delete in ((0,), (1, "", "no buffer\n"), OSError("gone")):
            with self.subTest(delete=delete):
                fake = Tmux(results={"paste-buffer": (1, "", "boom\n"), "delete-buffer": delete})
                with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
                    tmux_host.paste("s", "hi", proc=fake)
                buffer = fake.calls[0][3]
                self.assertEqual(
                    fake.calls[1:],
                    [
                        ["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", "=s:"],
                        ["tmux", "delete-buffer", "-b", buffer],
                    ],
                )
        fake = Tmux(fail={"load-buffer"})
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            tmux_host.paste("s", "hi", proc=fake)
        self.assertEqual(fake.commands(), ["load-buffer"])


class KeysTest(Case):
    def test_send_keys_names_the_keys(self):
        fake = Tmux()
        self.assertIsNone(tmux_host.keys("s", "Escape", "C-c", "Enter", proc=fake))
        self.assertEqual(fake.calls, [["tmux", "send-keys", "-t", "=s:", "Escape", "C-c", "Enter"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_a_name_tmux_would_misread_is_refused_before_tmux(self):
        for name in ("-l", "-", "Enter;", ";"):
            with self.subTest(name=name):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, "^tmux would misread"):
                    tmux_host.keys("s", "Escape", name, proc=fake)
                self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            tmux_host.keys("s", "Enter", proc=Tmux(fail={"send-keys"}))


class ReadTest(Case):
    def test_visible_pane_or_last_lines(self):
        for lines, printed, want, extra in (
            (None, "a\n  b\n\n \n", "a\n  b", []),
            (3, "1\n2\n\n3\n4\n\n\n", "\n3\n4", ["-S", "-3"]),
        ):
            with self.subTest(lines=lines):
                fake = Tmux(results={"capture-pane": (0, printed)})
                self.assertEqual(tmux_host.read("s", lines, proc=fake), want)
                self.assertEqual(
                    fake.calls, [["tmux", "capture-pane", "-p", "-J", "-t", "=s:", *extra]]
                )
                self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_lines_must_be_a_positive_int(self):
        fake = Tmux()
        for lines in (0, -1, True, 1.5, "3"):
            with self.subTest(lines=lines), self.assertRaises(tmux_host.TuiError):
                tmux_host.read("s", lines, proc=fake)
        self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            tmux_host.read("s", proc=Tmux(fail={"capture-pane"}))


class ShowTest(Case):
    def show(self, fake, template=None, env=None, session="s", **kw):
        self.stderr = io.StringIO()
        with environ(**(env or {})), which(), redirect_stderr(self.stderr):
            return tmux_host.show(
                session,
                template,
                services=tmux_host.PaneServices(proc=fake, script=tmux_host.__file__),
                **kw,
            )

    def test_attach_line_then_the_template(self):
        fake = Tmux()
        self.assertIsNone(self.show(fake, "open {{session}} --as {{session}}"))
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open s --as s"]])
        self.assertEqual(fake.kwargs, [SH_KW])
        self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_attach_line_takes_the_prefix(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env):
                self.show(Tmux(), "", env)
                self.assertEqual(
                    self.stderr.getvalue(), f"tui: session s: {prefix}tmux attach -t '=s'\n"
                )

    def test_session_quoted(self):
        fake = Tmux()
        self.show(fake, "open {{session}}", session="a;b c")
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open 'a;b c'"]])

    def test_failures_printed_and_returned(self):
        missing = FileNotFoundError(2, "No such file or directory", "/bin/sh")
        for template, result, why in (
            ("open {{name}} {{session}}", None, "unknown placeholder {{name}}"),
            ("false", (3,), "exit status 3"),
            ("sleep 99", subprocess.TimeoutExpired("/bin/sh", 30), "timed out after 30 s"),
            ("x", missing, str(missing)),
        ):
            with self.subTest(why=why):
                fake = Tmux(results={"/bin/sh": result} if result else {})
                self.assertEqual(self.show(fake, template), why)
                self.assertEqual(self.stderr.getvalue(), ATTACH + f"tui: show: {why}\n")
                self.assertEqual(len(fake.calls), 1 if result else 0)
