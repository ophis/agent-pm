import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tui  # noqa: E402

FORMAT = "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"
TMUX_KW = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}


def done(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


class Tmux:
    """Plays tmux: records each call. `pane` is what display-message prints (None: no server). On new-session it
    reads the handover file named in its argv, then unlinks it as the wrapper would, unless `hand_over` is False."""

    def __init__(self, pane=None, fail=(), hand_over=True):
        self.pane, self.fail, self.hand_over = pane, fail, hand_over
        self.calls, self.kwargs = [], []
        self.path = self.handover = self.mode = self.dir_mode = None

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        if argv[1] in self.fail:
            return done(argv, 1, err="boom\n")
        if argv[1] == "display-message":
            return done(argv, 1, err="no server running\n") if self.pane is None else done(argv, 0, self.pane + "\n")
        if argv[1] == "new-session":
            self.path = argv[argv.index(";") - 1]
            self.mode = stat.S_IMODE(os.stat(self.path).st_mode)
            self.dir_mode = stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode)
            with open(self.path) as f:
                self.handover = json.load(f)
            if self.hand_over:
                os.unlink(self.path)
        return done(argv)

    def commands(self):
        return [c[1] for c in self.calls]


class Start(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.bin, self.temp, self.cwd = (os.path.join(self.root, d) for d in ("bin", "temp", "cwd"))
        for d in (self.bin, self.temp, self.cwd):
            os.makedirs(d)
        self.tool = os.path.join(self.bin, "tool")
        with open(self.tool, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.tool, 0o755)
        for p in (unittest.mock.patch.object(tempfile, "tempdir", self.temp),
                  unittest.mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((120, 40)))):
            p.start()
            self.addCleanup(p.stop)
        self.sleep = unittest.mock.Mock()
        self.stderr = io.StringIO()

    def start(self, fake, session="s", argv=("tool", "-x", "a b"), env=None, **kw):
        env = {"PATH": self.bin, "HOME": "/h"} if env is None else env
        with redirect_stderr(self.stderr):
            tui.start(session, list(argv), cwd=self.cwd, env=env, proc=fake, sleep=self.sleep, **kw)

    def test_one_tmux_call_runs_the_wrapper_and_keeps_a_dead_pane(self):
        fake = Tmux()
        self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(fake.calls[1], ["tmux", "new-session", "-d", "-s", "s", "-x", "120", "-y", "40",
                                         sys.executable, "-I", "-c", tui.EXEC, fake.path,
                                         ";", "set-option", "-p", "-t", "=s:", "remain-on-exit", "on"])
        self.assertNotIn("-c", fake.calls[1][:fake.calls[1].index(sys.executable)])
        self.assertEqual([a for a in fake.calls[1] if a.endswith(";")], [";"])
        self.assertEqual(fake.kwargs, [TMUX_KW, TMUX_KW])
        self.sleep.assert_not_called()

    def test_handover_file(self):
        env = {"PATH": self.bin, "HOME": "/h", **{k: "x" for k in tui.TERMINAL_KEYS}}
        fake = Tmux()
        self.start(fake, env=env)
        self.assertEqual(fake.handover, {"argv": [self.tool, "-x", "a b"], "env": {"PATH": self.bin, "HOME": "/h"},
                                         "cwd": self.cwd})
        self.assertEqual(fake.mode, 0o600)
        self.assertEqual(fake.dir_mode, 0o700)
        self.assertEqual(os.path.dirname(os.path.dirname(fake.path)), self.temp)
        self.assertEqual(os.listdir(self.temp), [])

    def test_terminal_keys(self):
        self.assertEqual(set(tui.TERMINAL_KEYS), {
            "TMUX", "TMUX_PANE", "TERM", "COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "TERM_SESSION_ID",
            "ITERM_SESSION_ID", "ITERM_PROFILE", "LC_TERMINAL", "LC_TERMINAL_VERSION", "COLUMNS", "LINES"})

    def test_cwd_never_reaches_tmux(self):
        self.cwd = os.path.join(self.root, "#(x)#{y}")
        os.makedirs(self.cwd)
        fake = Tmux()
        self.start(fake)
        self.assertEqual(fake.handover["cwd"], self.cwd)
        self.assertEqual([a for c in fake.calls for a in c if "#(x)" in a or "#{y}" in a], [])

    def test_command_resolved_with_the_envs_path(self):
        fake = Tmux()
        with self.assertRaisesRegex(tui.TuiError, "sh"):
            self.start(fake, argv=("sh", "-c", "true"))
        with self.assertRaisesRegex(tui.TuiError, "no-such-tool"):
            self.start(fake, argv=("no-such-tool",))
        self.assertEqual(fake.calls, [])
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
        fake = Tmux(pane="0  ")
        with self.assertRaisesRegex(tui.TuiError, r"^session s is running; tmux attach -t '=s'$"):
            self.start(fake)
        self.assertEqual(fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]])
        self.assertEqual(os.listdir(self.temp), [])

    def test_dead_session_is_killed_then_started(self):
        fake = Tmux(pane="1 0 ")
        self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "kill-session", "new-session"])
        self.assertEqual(fake.calls[1], ["tmux", "kill-session", "-t", "=s"])

    def test_tmux_failure(self):
        fake = Tmux(fail={"new-session"})
        with self.assertRaisesRegex(tui.TuiError, r"^tmux: boom$"):
            self.start(fake)
        self.assertEqual(os.listdir(self.temp), [])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_tmux_missing(self):
        def missing(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory", "tmux")
        with self.assertRaisesRegex(tui.TuiError, r"^tmux: "):
            self.start(missing)
        self.assertEqual(os.listdir(self.temp), [])

    def test_wrapper_never_reads(self):
        for fail in ((), {"kill-session"}):
            with self.subTest(fail=fail):
                self.sleep.reset_mock()
                fake = Tmux(fail=fail, hand_over=False)
                with self.assertRaisesRegex(tui.TuiError, r"^the session did not start$"):
                    self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
                self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
                self.assertAlmostEqual(sum(c.args[0] for c in self.sleep.call_args_list), tui.HANDOVER_TIMEOUT)
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

    def test_unsafe_tmux_argument(self):
        for exe in ("/opt/py#3/bin/python3", "/opt/bin/python3;"):
            with self.subTest(exe=exe), unittest.mock.patch.object(sys, "executable", exe):
                fake = Tmux()
                with self.assertRaises(tui.TuiError):
                    self.start(fake)
                self.assertNotIn("new-session", fake.commands())
        hashed = os.path.join(self.root, "t#mp")
        os.makedirs(hashed)
        fake = Tmux()
        with unittest.mock.patch.object(tempfile, "tempdir", hashed), self.assertRaises(tui.TuiError):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(hashed), [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_invalid_session_name(self):
        for name in ("", "a b", "a.b", "a:b", "=a", "#{x}", "a;", "a\n", "é"):
            with self.subTest(name=name):
                fake = Tmux()
                for call in (lambda: self.start(fake, session=name), lambda: tui.status(name, proc=fake),
                             lambda: tui.kill(name, proc=fake)):
                    with self.assertRaises(tui.TuiError):
                        call()
                self.assertEqual(fake.calls, [])

    def test_shows_the_session_once_started(self):
        fake = Tmux()
        self.start(fake)
        self.assertEqual(self.stderr.getvalue(), "tui: session s: tmux attach -t '=s'\n")

    def test_show_gets_its_arguments_after_the_handover(self):
        fake = Tmux()
        seen = []

        def show(*args, **kw):
            seen.append((args, kw, os.path.exists(fake.path), fake.commands()))
        with unittest.mock.patch.object(tui, "show", side_effect=show):
            self.start(fake, show="tmpl", split="below", beside="b")
        self.assertEqual(seen, [(("s", "tmpl"), {"split": "below", "beside": "b", "proc": fake}, False,
                                 ["display-message", "new-session"])])


class Status(unittest.TestCase):
    def test_states(self):
        for pane, want in (("0  ", tui.RUNNING), ("1 3 ", 3), ("1 0 ", 0), ("1  kill", 137), ("1  9", 137),
                           ("  ", None), (None, None)):
            with self.subTest(pane=pane):
                fake = Tmux(pane=pane)
                self.assertEqual(tui.status("s", proc=fake), want)
                self.assertEqual(fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]])
                self.assertEqual(fake.kwargs, [TMUX_KW])


class Kill(unittest.TestCase):
    def test_kill(self):
        fake = Tmux()
        tui.kill("s", proc=fake)
        self.assertEqual(fake.calls, [["tmux", "kill-session", "-t", "=s"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_failure(self):
        with self.assertRaisesRegex(tui.TuiError, r"^tmux: boom$"):
            tui.kill("s", proc=Tmux(fail={"kill-session"}))


class Exec(unittest.TestCase):
    def test_safe_for_tmux(self):
        self.assertNotIn("#", tui.EXEC)
        self.assertFalse(tui.EXEC.rstrip().endswith(";"))

    def test_runs_the_handover(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            cwd = os.path.join(tmp, "cwd")
            os.makedirs(cwd)
            path = os.path.join(tmp, "handover.json")
            code = "import json, os, sys; print(json.dumps([os.getcwd(), dict(os.environ), sys.argv]))"
            env = {"A": "1", "B": "two words"}
            with open(path, "w") as f:
                json.dump({"argv": [sys.executable, "-c", code, "x", "y z"], "env": env, "cwd": cwd}, f)
            pane = {"TERM": "tmux-256color", "PANE_ONLY": "1", "PATH": os.environ.get("PATH", "")}
            res = subprocess.run([sys.executable, "-I", "-c", tui.EXEC, path], env=pane, capture_output=True,
                                 text=True, stdin=subprocess.DEVNULL, timeout=30)
            self.assertEqual(res.returncode, 0, res.stderr)
            got_cwd, got_env, got_argv = json.loads(res.stdout)
            self.assertEqual(got_cwd, cwd)
            self.assertEqual({k: got_env.get(k) for k in (*env, "TERM")}, {**env, "TERM": "tmux-256color"})
            self.assertNotIn("PANE_ONLY", got_env)
            self.assertNotIn("PATH", got_env)
            self.assertEqual(got_argv, ["-c", "x", "y z"])
            self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
