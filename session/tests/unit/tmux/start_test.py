"""Tmux start behavior at standard-library boundaries."""

import errno
import os
import re
import stat
import sys
import tempfile
import unittest
import unittest.mock

from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.model import types as tmux_model

from session.tests.unit.tmux.fixtures import StartFixture
from session.tests.unit.tmux.support import (
    FAILED,
    FORMAT,
    INSIDE,
    ITERM,
    PREFIXES,
    TMUX_KW,
    Tmux,
    chained,
    decorations,
    environ,
    which,
)


class StartTest(StartFixture):
    def test_start_runs_wrapper_decorates_dead_pane_without_opening_a_pane(
        self,
    ):
        # in tmux or iTerm2 all the same: start never shows the session
        for env in ({}, INSIDE, ITERM):
            with self.subTest(env=env), environ(**env), which():
                fake = Tmux()
                self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
                self.assertEqual(
                    fake.calls[1],
                    [
                        "tmux",
                        "new-session",
                        "-d",
                        "-s",
                        "s",
                        "-x",
                        "120",
                        "-y",
                        "40",
                        sys.executable,
                        "-I",
                        "-c",
                        tmux_host.EXEC,
                        fake.path,
                        fake.fifo,
                        ";",
                        "set-option",
                        "-p",
                        "-t",
                        "=s:",
                        "remain-on-exit",
                        "on",
                        *chained(decorations("s")),
                    ],
                )
                self.assertNotIn("-c", fake.calls[1][: fake.calls[1].index(sys.executable)])
                self.assertEqual({a for a in fake.calls[1] if a.endswith(";")}, {";"})
                self.assertEqual(fake.kwargs, [TMUX_KW] * len(fake.calls))
                self.sleep.assert_not_called()
                self.assertEqual(self.stderr.getvalue(), "")

    def test_handover_file(self):
        # the env as given but its terminal keys; PWD the cwd, given another (as here) or none
        # (test_events_and_...)
        env = {
            "PATH": self.bin,
            "HOME": "/h",
            "CHILD_SESSION": "1",
            "PWD": "/elsewhere",
            **{k: "x" for k in tmux_model.TERMINAL_KEYS},
        }
        for argv in (("tool", "-x", "a b"), ("tool", "--settings", "s.json")):
            with self.subTest(argv=argv):
                fake = Tmux()
                self.start(fake, argv=argv, env=env)
                self.assertEqual(
                    fake.handover,
                    {
                        "argv": [self.tool, *argv[1:]],
                        "env": {
                            "PATH": self.bin,
                            "HOME": "/h",
                            "CHILD_SESSION": "1",
                            "PWD": self.cwd,
                        },
                        "cwd": self.cwd,
                    },
                )
                self.assertEqual(fake.mode, 0o600)
                self.assertEqual(fake.dir_mode, 0o700)
                self.assertEqual(os.path.dirname(os.path.dirname(fake.path)), self.temp)
                self.assertEqual(os.path.dirname(fake.fifo), os.path.dirname(fake.path))
                self.assertTrue(stat.S_ISFIFO(fake.fifo_mode))
                self.assertEqual(stat.S_IMODE(fake.fifo_mode), 0o600)
                self.assertEqual(os.listdir(self.temp), [])

    def test_terminal_keys(self):
        self.assertEqual(
            set(tmux_model.TERMINAL_KEYS),
            {
                "TMUX",
                "TMUX_PANE",
                "TERM",
                "COLORTERM",
                "TERM_PROGRAM",
                "TERM_PROGRAM_VERSION",
                "TERM_SESSION_ID",
                "ITERM_SESSION_ID",
                "ITERM_PROFILE",
                "LC_TERMINAL",
                "LC_TERMINAL_VERSION",
                "COLUMNS",
                "LINES",
            },
        )

    def test_cwd_never_reaches_tmux(self):
        self.cwd = os.path.join(self.root, "#(x)#{y}")
        os.makedirs(self.cwd)
        fake = Tmux()
        self.start(fake)
        self.assertEqual(fake.handover["cwd"], self.cwd)
        self.assertEqual([a for c in fake.calls for a in c if "#(x)" in a or "#{y}" in a], [])

    def test_refused_before_tmux(self):
        # the command resolved with the env's PATH (no sh there); an events file that is no regular
        # file
        link = os.path.join(self.root, "link")
        os.symlink(self.tool, link)
        for kw, error in (
            ({"argv": ("sh", "-c", "true")}, "sh"),
            ({"argv": ("no-such-tool",)}, "no-such-tool"),
            ({"argv": ()}, "no command"),
            ({"events": self.cwd}, ""),
            ({"events": link}, ""),
        ):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, error):
                    self.start(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_unwritable(self):
        fake = Tmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", os.path.join(self.root, "missing")),
            self.assertRaisesRegex(tmux_host.TuiError, "^handover: "),
        ):
            self.start(fake)
        real = os.open

        def refuse(path, *args, **kw):
            if str(path).endswith("handover.json"):
                raise PermissionError(13, "Permission denied", path)
            return real(path, *args, **kw)

        with (
            unittest.mock.patch("os.open", side_effect=refuse),
            self.assertRaisesRegex(tmux_host.TuiError, "^handover: "),
        ):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(self.temp), [])

    def test_relative_command_made_absolute(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        for argv0, path in (("bin/tool", "/nonexistent"), ("tool", "bin")):
            fake = Tmux()
            self.start(fake, argv=(argv0,), env={"PATH": path})
            self.assertEqual(fake.handover["argv"], [self.tool])

    def test_live_session_is_refused(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env), unittest.mock.patch.dict(os.environ, env):
                fake = Tmux(pane="0  ")
                with self.assertRaisesRegex(
                    tmux_host.TuiError,
                    "^" + re.escape(f"session s is running; {prefix}tmux attach -t '=s'") + "$",
                ):
                    self.start(fake)
                self.assertEqual(
                    fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_dead_session_is_killed_then_started(self):
        for pane in ("1 0 ", "1  9"):
            with self.subTest(pane=pane):
                fake = Tmux(pane=pane)
                self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "kill-session", "new-session"]
                )
                self.assertEqual(fake.calls[1], ["tmux", "kill-session", "-t", "=s"])

    def test_an_unknown_session_is_never_killed(self):
        # tmux failing otherwise: new-session finds out; another start's session is not ours to kill
        for err in FAILED:
            with self.subTest(err=err):
                fake = Tmux(results={"display-message": (1, "", err)})
                self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
        fake = Tmux(
            results={
                "display-message": (1, "", "boom\n"),
                "new-session": (1, "", "duplicate session: s\n"),
            }
        )
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: duplicate session: s$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_tags_are_session_options_set_in_the_new_session_call(self):
        fake = Tmux()
        self.start(fake, tags={"@sid": "0123abcd-x", "@k": "a b#{x}"})
        self.assertEqual(
            fake.calls[1][fake.calls[1].index(fake.fifo) + 1 :],
            [
                ";",
                "set-option",
                "-p",
                "-t",
                "=s:",
                "remain-on-exit",
                "on",
                ";",
                "set-option",
                "-t",
                "=s:",
                "@sid",
                "0123abcd-x",
                ";",
                "set-option",
                "-t",
                "=s:",
                "@k",
                "a b#{x}",
                *chained(decorations("s")),
            ],
        )
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_bad_tags_refused_before_tmux(self):
        for tags in (
            {"sid": "x"},
            {"@": "x"},
            {"@a b": "x"},
            {"@a;": "x"},
            {"@k": "x;"},
            {"@k": 1},
            {"@k": None},
        ):
            with self.subTest(tags=tags):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, "^tag "):
                    self.start(fake, tags=tags)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])

    def test_a_failed_tmux_call_kills_the_session_it_may_have_made(self):
        fake = Tmux(
            fail={"new-session"}
        )  # e.g. the chained set-option failed after new-session made the session
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
        self.assertEqual(os.listdir(self.temp), [])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_a_duplicate_session_is_not_killed(self):
        fake = Tmux(
            results={"new-session": (1, "", "duplicate session: s\n")}
        )  # another start took the name
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: duplicate session: s$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_an_interrupt_after_new_session_kills_the_session(self):
        def interrupt(*_args, **_kw):
            raise KeyboardInterrupt

        fake = Tmux(hand_over=False)
        self.sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertIs(fake.dir_at_kill, expr2=False)
        self.assertEqual(os.listdir(self.temp), [])

    def test_tmux_missing(self):
        # status: unknown; new-session: the error
        def missing(_argv, **_kw):
            raise FileNotFoundError(2, "No such file or directory", "tmux")

        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: "):
            self.start(missing)
        self.assertEqual(os.listdir(self.temp), [])

    def test_wrapper_never_reads(self):
        for fail in ((), {"kill-session"}):
            with self.subTest(fail=fail):
                self.sleep.reset_mock()
                fake = Tmux(fail=fail, hand_over=False)
                with self.assertRaisesRegex(tmux_host.TuiError, r"^the session did not start$"):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
                self.assertIs(fake.dir_at_kill, expr2=False)
                self.assertAlmostEqual(
                    sum(c.args[0] for c in self.sleep.call_args_list), tmux_model.HANDOVER_TIMEOUT
                )
                self.assertEqual(os.listdir(self.temp), [])
                self.assertEqual(self.stderr.getvalue(), "")

    def test_waits_for_a_late_wrapper(self):
        fake = Tmux(hand_over=False)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                os.unlink(fake.path)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_waits_for_a_late_exec(self):
        fake, slept = Tmux(wrapper="hang"), []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                os.close(fake.writer)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_an_exec_failure_raises_its_error_and_kills_the_session(self):
        fake = Tmux(wrapper=errno.ENOEXEC)
        with self.assertRaisesRegex(
            tmux_host.TuiError,
            "^" + re.escape(f"cannot run {self.tool}: {os.strerror(errno.ENOEXEC)}") + "$",
        ):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_a_handover_at_its_last_poll_still_gets_the_whole_exec_wait(self):
        fake, slept = Tmux(hand_over=False), []
        last = round(tmux_model.HANDOVER_TIMEOUT / tmux_model.POLL)

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == last:
                fake.writer = os.open(fake.fifo, os.O_WRONLY | os.O_NONBLOCK)
                os.unlink(fake.path)
            elif len(slept) == last + 3:
                os.close(fake.writer)

        self.sleep = sleep
        self.start(fake)
        self.assertEqual(len(slept), last + 3)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])

    def test_bad_errpipe_data_is_an_error_and_kills_the_session(self):
        for data in (b"x", b"9" * 20):
            with self.subTest(data=data):
                fake = Tmux(wrapper=data)
                with self.assertRaisesRegex(
                    tmux_host.TuiError,
                    "^" + re.escape(f"cannot run {self.tool}: bad errpipe data") + "$",
                ):
                    self.start(fake)
                self.assertEqual(
                    fake.commands(), ["display-message", "new-session", "kill-session"]
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_never_execd_is_not_started(self):
        fake = Tmux(wrapper="hang")
        with self.assertRaisesRegex(tmux_host.TuiError, r"^the session did not start$"):
            self.start(fake)
        os.close(fake.writer)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertAlmostEqual(
            sum(c.args[0] for c in self.sleep.call_args_list), tmux_model.HANDOVER_TIMEOUT
        )
        self.assertEqual(os.listdir(self.temp), [])

    def test_unsafe_tmux_argument(self):
        for exe in ("/opt/py#3/bin/python3", "/opt/bin/python3;"):
            with self.subTest(exe=exe), unittest.mock.patch.object(sys, "executable", exe):
                fake = Tmux()
                with self.assertRaises(tmux_host.TuiError):
                    self.start(fake)
                self.assertNotIn("new-session", fake.commands())
        hashed = os.path.join(self.root, "t#mp")
        os.makedirs(hashed)
        fake = Tmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", hashed),
            self.assertRaises(tmux_host.TuiError),
        ):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(hashed), [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_invalid_session_name(self):
        for name in ("", "a b", "a.b", "a:b", "=a", "#{x}", "a;", "a\n", "é"):
            with self.subTest(name=name):
                fake = Tmux()
                for call in (
                    lambda *, fake=fake, name=name: self.start(fake, session=name),
                    lambda *, fake=fake, name=name: tmux_host.status(name, proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.detach(
                        name,
                        tmux_host.Command(argv=["tool"], cwd=self.cwd, env={"PATH": self.bin}),
                        proc=fake,
                        sleep=self.sleep,
                    ),
                    lambda *, fake=fake, name=name: tmux_host.kill(name, proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.send(
                        name, "x", proc=fake, sleep=self.sleep
                    ),
                    lambda *, fake=fake, name=name: tmux_host.paste(name, "x", proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.keys(name, "Enter", proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.read(name, proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.option(name, "@sid", proc=fake),
                    lambda *, fake=fake, name=name: tmux_host.pane(name, proc=fake),
                ):
                    with self.assertRaises(tmux_host.TuiError):
                        call()
                self.assertEqual(fake.calls, [])

    def test_events_and_decorations(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        events = os.path.join(self.root, "ev")
        # the status line is the caller's choice
        for given, path, status_line, status in (
            (None, None, False, "off"),
            ("ev", events, False, "off"),
            (None, None, True, "on"),
        ):
            with self.subTest(events=given, status_line=status_line):
                fake = Tmux()
                self.start(fake, env={"PATH": self.bin}, events=given, status_line=status_line)
                self.assertEqual(fake.handover["argv"], [self.tool, "-x", "a b"])
                self.assertEqual(fake.handover["env"], {"PATH": self.bin, "PWD": self.cwd})
                self.assertEqual(fake.commands(), ["display-message", "new-session"])
                self.assertEqual(
                    fake.calls[1][fake.calls[1].index(fake.fifo) + 8 :],
                    chained(decorations("s", path, status)),
                )
        self.assertEqual(stat.S_IMODE(os.stat(events).st_mode), 0o600)
