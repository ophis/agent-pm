"""Tmux detach behavior at standard-library boundaries."""

import errno
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import suppress

from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.tmux.model import types as tmux_model

from session.tests.case import Case
from session.tests.unit.tmux.fixtures import StartFixture
from session.tests.unit.tmux.support import (
    SIGNALS,
    TMUX_KW,
    WAIT,
    Tmux,
)


class DetachTest(StartFixture):
    """Session d hosting argv as old drive.detach did: no decoration, no dead pane kept."""

    def detach(self, fake, session="d", argv=("tool", "-x", "a b"), env=None, **kw):
        env = {"PATH": self.bin, "HOME": "/h"} if env is None else env
        tmux_host.detach(
            session,
            tmux_host.Command(argv=list(argv), cwd=self.cwd, env=env),
            options=tmux_host.DetachOptions(**kw),
            proc=fake,
            sleep=self.sleep,
        )

    def test_new_session_runs_the_wrapper_and_returns_once_argv_execd(self):
        fake = Tmux()
        self.assertIsNone(self.detach(fake))
        self.assertEqual(
            fake.calls,
            [
                [
                    "tmux",
                    "new-session",
                    "-d",
                    "-s",
                    "d",
                    sys.executable,
                    "-I",
                    "-c",
                    tmux_host.EXEC,
                    fake.path,
                    fake.fifo,
                ]
            ],
        )
        self.assertEqual(fake.kwargs, [TMUX_KW])
        self.assertNotIn("remain-on-exit", fake.calls[0])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_with_the_blocked_signals(self):
        env = {
            "PATH": self.bin,
            "HOME": "/h",
            "CHILD_SESSION": "1",
            "PWD": "/elsewhere",
            **{k: "x" for k in tmux_model.TERMINAL_KEYS},
        }
        for block, want in (((), []), ((signal.SIGHUP, signal.SIGTERM, signal.SIGINT), SIGNALS)):
            with self.subTest(block=block):
                fake = Tmux()
                self.detach(fake, env=env, block=block)
                self.assertEqual(
                    fake.handover,
                    {
                        "argv": [self.tool, "-x", "a b"],
                        "cwd": self.cwd,
                        "env": {
                            "PATH": self.bin,
                            "HOME": "/h",
                            "CHILD_SESSION": "1",
                            "PWD": self.cwd,
                        },
                        "block": want,
                    },
                )
                self.assertEqual(fake.mode, 0o600)
                self.assertEqual(fake.dir_mode, 0o700)
                self.assertEqual(os.listdir(self.temp), [])

    def test_the_envs_iterm_session_id_goes_to_the_session_only_in_its_iterm2_form(self):
        for value, given in (
            ("w0t0p0:ABC", True),
            ("w0t1p0:6D7E-8F", True),
            (None, False),
            ("", False),
            ("w0t0p0", False),
            ("cmd", False),
            ("w0t0p0:a;b", False),
            ("w0t0p0:ABC\n", False),
            (":ABC", False),
            ("w0t0p0:A_B", False),
        ):
            with self.subTest(value=value):
                fake = Tmux()
                env = {"PATH": self.bin, **({} if value is None else {"ITERM_SESSION_ID": value})}
                self.detach(fake, env=env)
                head = [
                    "tmux",
                    "new-session",
                    "-d",
                    *(["-e", f"ITERM_SESSION_ID={value}"] if given else []),
                    "-s",
                    "d",
                ]
                self.assertEqual(fake.calls[0][: len(head) + 1], [*head, sys.executable])
                self.assertNotIn("ITERM_SESSION_ID", fake.handover["env"])

    def test_opener_set_in_the_same_call(self):
        for opener in ("mgr", "w0t0p0:ABC"):
            with self.subTest(opener=opener):
                fake = Tmux()
                self.detach(fake, opener=opener)
                self.assertEqual(
                    fake.calls,
                    [
                        [
                            "tmux",
                            "new-session",
                            "-d",
                            "-s",
                            "d",
                            sys.executable,
                            "-I",
                            "-c",
                            tmux_host.EXEC,
                            fake.path,
                            fake.fifo,
                            ";",
                            "set-option",
                            "-t",
                            "=d:",
                            "@opener",
                            opener,
                        ]
                    ],
                )

    def test_an_existing_session_is_refused_untouched(self):
        fake = Tmux(results={"new-session": (1, "", "duplicate session: d\n")})
        with self.assertRaisesRegex(tmux_host.TuiError, r"^session d exists$"):
            self.detach(fake, opener="mgr")
        self.assertEqual(fake.commands(), ["new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_not_taken_kills_it(self):
        for fail in ((), {"kill-session"}):
            with self.subTest(fail=fail):
                self.sleep.reset_mock()
                fake = Tmux(fail=fail, hand_over=False)
                with self.assertRaisesRegex(tmux_host.TuiError, r"^session d did not start$"):
                    self.detach(fake)
                self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
                self.assertIs(fake.dir_at_kill, expr2=False)
                self.assertAlmostEqual(
                    sum(c.args[0] for c in self.sleep.call_args_list), tmux_model.HANDOVER_TIMEOUT
                )
                self.assertEqual(os.listdir(self.temp), [])

    def test_an_exec_failure_raises_its_error_and_kills_the_session(self):
        fake = Tmux(wrapper=errno.ENOENT)
        with self.assertRaisesRegex(
            tmux_host.TuiError,
            "^" + re.escape(f"cannot run {self.tool}: {os.strerror(errno.ENOENT)}") + "$",
        ):
            self.detach(fake)
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.sleep.assert_not_called()
        self.assertEqual(os.listdir(self.temp), [])

    def test_never_execd_kills_it(self):
        fake = Tmux(wrapper="hang")
        with self.assertRaisesRegex(tmux_host.TuiError, r"^session d did not start$"):
            self.detach(fake)
        os.close(fake.writer)
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.assertAlmostEqual(
            sum(c.args[0] for c in self.sleep.call_args_list), tmux_model.HANDOVER_TIMEOUT
        )
        self.assertEqual(os.listdir(self.temp), [])

    def test_a_failed_tmux_call_kills_the_session_it_may_have_made(self):
        fake = Tmux(
            fail={"new-session"}
        )  # e.g. the chained set-option failed after new-session made the session
        with self.assertRaisesRegex(tmux_host.TuiError, r"^tmux: boom$"):
            self.detach(fake, opener="mgr")
        self.assertEqual(fake.calls[1:], [["tmux", "kill-session", "-t", "=d"]])
        self.assertEqual(os.listdir(self.temp), [])

    def test_an_interrupt_after_new_session_kills_the_session(self):
        def interrupt(*_args, **_kw):
            raise KeyboardInterrupt

        fake, self.sleep = Tmux(hand_over=False), interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.detach(fake)
        self.assertEqual(fake.commands(), ["new-session", "kill-session"])
        self.assertIs(fake.dir_at_kill, expr2=False)
        self.assertEqual(os.listdir(self.temp), [])

    def test_refused_before_tmux(self):
        for kw, error in (
            ({"argv": ()}, "^no command$"),
            ({"argv": ("no-such-tool",)}, "no-such-tool"),
            ({"argv": ("tool", "a\0b")}, "NUL"),
            ({"argv": ("tool", "\ud800")}, "encoded"),
            ({"opener": "a b"}, "opener"),
            ({"opener": "a:b:c"}, "opener"),
            ({"opener": ""}, "opener"),
        ):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaisesRegex(tmux_host.TuiError, error):
                    self.detach(fake, **kw)
                self.assertEqual(fake.calls, [])
                self.assertEqual(os.listdir(self.temp), [])
        with (
            unittest.mock.patch.object(sys, "executable", "/opt/py#3/bin/python3"),
            self.assertRaisesRegex(tmux_host.TuiError, "tmux would misread"),
        ):
            self.detach(fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_handover_file_unwritable(self):
        fake = Tmux()
        with (
            unittest.mock.patch.object(tempfile, "tempdir", os.path.join(self.root, "missing")),
            self.assertRaisesRegex(tmux_host.TuiError, "^handover: "),
        ):
            self.detach(fake)
        self.assertEqual(fake.calls, [])

    def test_cwd_never_reaches_tmux(self):
        self.cwd = os.path.join(self.root, "#(x)#{y}")
        os.makedirs(self.cwd)
        fake = Tmux()
        self.detach(fake)
        self.assertEqual(fake.handover["cwd"], self.cwd)
        self.assertEqual([a for c in fake.calls for a in c if "#(x)" in a or "#{y}" in a], [])


class ExecTest(Case):
    def test_safe_for_tmux(self):
        self.assertNotIn("#", tmux_host.EXEC)
        self.assertFalse(tmux_host.EXEC.rstrip().endswith(";"))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.path, self.fifo = (os.path.join(self.root, n) for n in ("handover.json", "errpipe"))
        os.mkfifo(self.fifo, 0o600)
        self.reader = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, self.reader)

    def run_exec(self, argv, env, cwd, pane, prelude="", **extra):
        """The wrapper as tmux runs it, given a handover file naming argv and the errpipe; `prelude`
        runs before it.
        """
        with open(self.path, "w") as f:
            json.dump({"argv": argv, "env": env, "cwd": cwd, **extra}, f)
        return subprocess.run(
            self.wrapper(prelude),
            env=pane,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )

    def wrapper(self, prelude=""):
        return [sys.executable, "-I", "-c", prelude + tmux_host.EXEC, self.path, self.fifo]

    def piped(self):
        """What the wrapper wrote to the errpipe, read to EOF; BlockingIOError while a writer holds
        it open.
        """
        out = b""
        while chunk := os.read(self.reader, 64):
            out += chunk
        return out

    def test_runs_the_handover(self):
        cwd = os.path.join(self.root, "cwd")
        os.makedirs(cwd)
        code = "import json, os, sys; print(json.dumps([os.getcwd(), dict(os.environ), sys.argv]))"
        env = {"A": "1", "B": "two words"}
        pane = {"TERM": "tmux-256color", "PANE_ONLY": "1", "PATH": os.environ.get("PATH", "")}
        res = self.run_exec([sys.executable, "-c", code, "x", "y z"], env, cwd, pane)
        self.assertEqual(res.returncode, 0, res.stderr)
        got_cwd, got_env, got_argv = json.loads(res.stdout)
        self.assertEqual(got_cwd, cwd)
        self.assertEqual(
            {k: got_env.get(k) for k in (*env, "TERM")}, {**env, "TERM": "tmux-256color"}
        )
        self.assertNotIn("PANE_ONLY", got_env)
        self.assertNotIn("PATH", got_env)
        self.assertEqual(got_argv, ["-c", "x", "y z"])
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.piped(), b"")

    def test_execve_closes_the_errpipe_while_argv_runs(self):
        """EOF with no data once argv runs: the wrapper's end never reaches argv."""
        go = os.path.join(self.root, "go")
        with open(self.path, "w") as f:
            json.dump({"argv": [sys.executable, "-c", WAIT, go], "env": {}, "cwd": self.root}, f)
        wrapper = subprocess.Popen(
            self.wrapper(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        self.addCleanup(lambda: wrapper.returncode is None and wrapper.kill())
        piped, deadline = None, time.monotonic() + 10
        while piped is None and time.monotonic() < deadline:
            time.sleep(0.01)
            if not os.path.exists(self.path):  # a FIFO no writer opened yet reads as EOF too
                with suppress(BlockingIOError):
                    piped = self.piped()
        self.assertEqual(piped, b"")
        self.assertIsNone(wrapper.poll())
        open(go, "w").close()
        out, err = wrapper.communicate(timeout=30)
        self.assertEqual((wrapper.returncode, out), (0, ""), err)

    def test_an_exec_failure_writes_its_errno_and_argv_never_runs(self):
        for text, number in (
            ("#!/nonexistent/interpreter\nprint('ran')\n", errno.ENOENT),
            ("echo ran\n", errno.ENOEXEC),
        ):
            with self.subTest(text=text):
                program = os.path.join(self.root, "program")
                with open(program, "w") as f:
                    f.write(text)
                os.chmod(program, 0o755)
                res = self.run_exec([program], {}, self.root, {})
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertEqual(self.piped(), b"%d" % number)
                self.assertFalse(os.path.exists(self.path))

    def test_any_other_exec_failure_writes_einval_and_argv_never_runs(self):
        for env in ({"X": "a\0b"}, {"X": "\ud800"}, {"X=Y": "1"}):
            with self.subTest(env=env):
                res = self.run_exec([sys.executable, "-c", "print('ran')"], env, self.root, {})
                self.assertEqual((res.returncode, res.stdout), (1, ""))
                self.assertIn("Error: ", res.stderr)
                self.assertEqual(self.piped(), b"%d" % errno.EINVAL)

    def test_a_cwd_it_cannot_enter_fails_it_leaving_the_handover_untaken(self):
        res = self.run_exec(
            [sys.executable, "-c", "print('ran')"], {}, os.path.join(self.root, "missing"), {}
        )
        self.assertEqual((res.returncode, res.stdout), (1, ""))
        self.assertIn("FileNotFoundError", res.stderr)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(self.piped(), b"%d" % errno.ENOENT)

    def test_an_errpipe_without_a_reader_runs_nothing_leaving_the_handover_untaken(self):
        """The caller gone: ENXIO."""
        self.fifo = os.path.join(self.root, "unread")
        os.mkfifo(self.fifo, 0o600)
        res = self.run_exec([sys.executable, "-c", "print('ran')"], {}, self.root, {})
        self.assertEqual((res.returncode, res.stdout), (1, ""))
        self.assertIn(f"[Errno {errno.ENXIO}]", res.stderr)
        self.assertTrue(os.path.exists(self.path))

    def test_block_signals_stay_blocked_in_argv(self):
        code = (
            "import json, signal; "
            "print(json.dumps(sorted(signal.pthread_sigmask(signal.SIG_BLOCK, "
            "[]))))"
        )
        for extra, want in (
            ({}, set()),
            ({"block": [signal.SIGHUP, signal.SIGTERM]}, {signal.SIGHUP, signal.SIGTERM}),
        ):
            with self.subTest(**extra):
                res = self.run_exec([sys.executable, "-c", code], {}, self.root, {}, **extra)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(
                    set(json.loads(res.stdout)) & {signal.SIGHUP, signal.SIGTERM}, want
                )

    def test_default_signal_dispositions(self):
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            with self.subTest(sig=sig.name):
                res = self.run_exec(
                    ["/bin/sh", "-c", f"kill -{sig.name[3:]} $$"], {}, self.root, {}
                )
                self.assertEqual(res.returncode, -sig, res.stderr)
