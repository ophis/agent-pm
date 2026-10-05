import ast
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tui  # noqa: E402

FORMAT = "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"
CLIENTS = "#{client_activity} #{client_tty}"
TMUX_KW = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}
SH_KW = {"stdin": subprocess.DEVNULL, "timeout": 30}
OSA_KW = {**TMUX_KW, "timeout": 30}
TMUX = '/opt/a"b\\c/bin/tmux'   # the script text must never carry it
ATTACH = "tui: session s: tmux attach -t '=s'\n"


def done(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


def environ(**values):
    return unittest.mock.patch.dict(os.environ, values, clear=True)


def which(tmux=TMUX):
    real = shutil.which
    return unittest.mock.patch("shutil.which", side_effect=lambda cmd, *a, **kw: tmux if cmd == "tmux"
                               else real(cmd, *a, **kw))


def osa(split, kind, session, *anchors):
    return ["osascript", "-e", tui.APPLESCRIPT, split, kind, TMUX, session, *anchors]


class Tmux:
    """Plays tmux, and sh and osascript: records each call. `results` maps a tmux command or another program to its
    (rc, stdout, stderr) or an exception to raise. `pane` is what display-message prints (None: no server). On
    new-session it reads the handover file named in its argv, then unlinks it as the wrapper would, unless `hand_over`
    is False."""

    def __init__(self, pane=None, fail=(), hand_over=True, results=None):
        self.pane, self.fail, self.hand_over, self.results = pane, fail, hand_over, results or {}
        self.calls, self.kwargs = [], []
        self.path = self.handover = self.mode = self.dir_mode = self.dir_at_kill = None

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        key = argv[1] if argv[0] == "tmux" else argv[0]
        if key in self.results:
            result = self.results[key]
            if isinstance(result, BaseException):
                raise result
            return done(argv, *result)
        if argv[1] == "kill-session" and self.path:
            self.dir_at_kill = os.path.isdir(os.path.dirname(self.path))
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
        return [c[1] if c[0] == "tmux" else c[0] for c in self.calls]


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
                  unittest.mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((120, 40))),
                  unittest.mock.patch.dict(os.environ, {"TUI_SHOW": ""})):
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
        env = {"PATH": self.bin, "HOME": "/h", "CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1",
               **{k: "x" for k in tui.TERMINAL_KEYS}}
        fake = Tmux()
        self.start(fake, env=env)
        self.assertEqual(fake.handover, {"argv": [self.tool, "-x", "a b"],
                                         "env": {"PATH": self.bin, "HOME": "/h", "CLAUDECODE": "1"}, "cwd": self.cwd})
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

    def test_empty_argv(self):
        fake = Tmux()
        with self.assertRaisesRegex(tui.TuiError, "no command"):
            self.start(fake, argv=())
        self.assertEqual(fake.calls, [])

    def test_handover_file_unwritable(self):
        fake = Tmux()
        with unittest.mock.patch.object(tempfile, "tempdir", os.path.join(self.root, "missing")), \
                self.assertRaisesRegex(tui.TuiError, "^handover: "):
            self.start(fake)
        real = os.open

        def refuse(path, *args, **kw):
            if str(path).endswith("handover.json"):
                raise PermissionError(13, "Permission denied", path)
            return real(path, *args, **kw)
        with unittest.mock.patch("os.open", side_effect=refuse), self.assertRaisesRegex(tui.TuiError, "^handover: "):
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

    def test_a_failed_tmux_call_kills_the_session_it_may_have_made(self):
        fake = Tmux(fail={"new-session"})   # e.g. the chained set-option failed after new-session made the session
        with self.assertRaisesRegex(tui.TuiError, r"^tmux: boom$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])

    def test_a_duplicate_session_is_not_killed(self):
        fake = Tmux(results={"new-session": (1, "", "duplicate session: s\n")})   # another start took the name
        with self.assertRaisesRegex(tui.TuiError, r"^tmux: duplicate session: s$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_an_interrupt_after_new_session_kills_the_session(self):
        def interrupt(*args, **kw):
            raise KeyboardInterrupt
        for where in ("handover", "show"):
            with self.subTest(where=where):
                fake = Tmux(hand_over=where == "show")
                self.sleep = interrupt
                with unittest.mock.patch.object(tui, "show", side_effect=interrupt), \
                        self.assertRaises(KeyboardInterrupt):
                    self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
                self.assertIs(fake.dir_at_kill, False)
                self.assertEqual(os.listdir(self.temp), [])

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
                self.assertIs(fake.dir_at_kill, False)
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
                             lambda: tui.kill(name, proc=fake), lambda: tui.send(name, "x", proc=fake, sleep=self.sleep),
                             lambda: tui.read(name, proc=fake)):
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
            self.start(fake, template="tmpl", split="below", beside="b")
        self.assertEqual(seen, [(("s", "tmpl"), {"split": "below", "beside": "b", "proc": fake}, False,
                                 ["display-message", "new-session"])])

    def test_tui_show_comes_from_os_environ_not_env(self):
        fake = Tmux()
        self.start(fake, env={"PATH": self.bin, "TUI_SHOW": "echo env"})
        self.assertEqual(fake.commands(), ["display-message", "new-session"])
        self.assertEqual(fake.handover["env"]["TUI_SHOW"], "echo env")
        fake = Tmux()
        with unittest.mock.patch.dict(os.environ, {"TUI_SHOW": "echo {{session}}"}):
            self.start(fake)
        self.assertEqual(fake.calls[-1], ["/bin/sh", "-c", "echo s"])

    def test_a_failed_show_is_only_printed(self):
        fake = Tmux(results={"osascript": FileNotFoundError(2, "No such file or directory", "osascript")})
        with environ(ITERM_SESSION_ID="w0t0p0:ABC"), which():
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "osascript"])
        self.assertEqual(self.stderr.getvalue(),
                         ATTACH + "tui: show: osascript: [Errno 2] No such file or directory: 'osascript'\n")

    def test_bad_layout_refused_before_tmux(self):
        with environ(ITERM_SESSION_ID="w0t0p0:ABC"), which():
            for kw in ({"split": "up"}, {"beside": "a b"}):
                with self.subTest(**kw):
                    fake = Tmux()
                    with self.assertRaises(tui.TuiError):
                        self.start(fake, **kw)
                    self.assertEqual(fake.calls, [])
            fake = Tmux()
            self.start(fake, template="", split="up", beside="a b")
            self.assertEqual(fake.commands(), ["display-message", "new-session"])


class Status(unittest.TestCase):
    def test_states(self):
        for pane, want in (("0  ", tui.RUNNING), ("1 3 ", 3), ("1 0 ", 0), ("1  kill", 137), ("1  9", 137),
                           ("1  ", tui.RUNNING), ("  ", None), (None, None)):
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


class Send(unittest.TestCase):
    def test_types_the_text_then_presses_enter(self):
        fake = Tmux()
        tui.send("s", "-x hi", proc=fake, sleep=lambda seconds: fake.calls.append(("sleep", seconds)))
        self.assertEqual(fake.calls, [["tmux", "send-keys", "-t", "=s:", "-l", "--", "-x hi"], ("sleep", 0.5),
                                      ["tmux", "send-keys", "-t", "=s:", "Enter"]])
        self.assertEqual(fake.kwargs, [TMUX_KW, TMUX_KW])
        self.assertEqual(tui.PAUSE, 0.5)

    def test_trailing_semicolon_escaped(self):
        for text, sent in (("ls;", "ls\\;"), (";", "\\;"), ("a;b", "a;b"), ("a\\;", "a\\\\;")):
            with self.subTest(text=text):
                fake = Tmux()
                tui.send("s", text, proc=fake, sleep=lambda seconds: None)
                self.assertEqual(fake.calls[0][-1], sent)

    def test_failure(self):
        fake, sleep = Tmux(fail={"send-keys"}), unittest.mock.Mock()
        with self.assertRaisesRegex(tui.TuiError, r"^tmux: boom$"):
            tui.send("s", "hi", proc=fake, sleep=sleep)
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()


class Read(unittest.TestCase):
    def test_visible_pane(self):
        fake = Tmux(results={"capture-pane": (0, "a\n  b\n\n \n")})
        self.assertEqual(tui.read("s", proc=fake), "a\n  b")
        self.assertEqual(fake.calls, [["tmux", "capture-pane", "-p", "-J", "-t", "=s:"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_last_lines(self):
        fake = Tmux(results={"capture-pane": (0, "1\n2\n\n3\n4\n\n\n")})
        self.assertEqual(tui.read("s", 3, proc=fake), "\n3\n4")
        self.assertEqual(fake.calls, [["tmux", "capture-pane", "-p", "-J", "-t", "=s:", "-S", "-3"]])

    def test_lines_must_be_a_positive_int(self):
        fake = Tmux()
        for lines in (0, -1, True, 1.5, "3"):
            with self.subTest(lines=lines), self.assertRaises(tui.TuiError):
                tui.read("s", lines, proc=fake)
        self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(tui.TuiError, r"^tmux: boom$"):
            tui.read("s", proc=Tmux(fail={"capture-pane"}))


class Show(unittest.TestCase):
    def show(self, fake, template=None, env=None, session="s", **kw):
        self.stderr = io.StringIO()
        with environ(**(env or {})), which(), redirect_stderr(self.stderr):
            return tui.show(session, template, proc=fake, **kw)

    def test_constants(self):
        self.assertEqual((tui.SHOW_TIMEOUT, tui.PLACEHOLDERS), (30, {"session"}))

    def test_attach_line_then_the_template(self):
        fake = Tmux()
        self.assertIsNone(self.show(fake, "open {{session}} --as {{session}}"))
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open s --as s"]])
        self.assertEqual(fake.kwargs, [SH_KW])
        self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_session_quoted(self):
        fake = Tmux()
        self.show(fake, "open {{session}}", session="a;b c")
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open 'a;b c'"]])

    def test_argument_beats_tui_show_beats_iterm(self):
        iterm = {"ITERM_SESSION_ID": "w0t0p0:ABC"}
        for template, env, want in (("echo arg", {"TUI_SHOW": "echo env", **iterm}, ["/bin/sh", "-c", "echo arg"]),
                                    (None, {"TUI_SHOW": "echo env", **iterm}, ["/bin/sh", "-c", "echo env"]),
                                    (None, iterm, osa("right", "id", "s", "ABC"))):
            with self.subTest(template=template, env=env):
                fake = Tmux(results={"osascript": (0, "\n")})
                self.assertIsNone(self.show(fake, template, env))
                self.assertEqual(fake.calls, [want])
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_empty_runs_nothing(self):
        for template, env in (("", {"TUI_SHOW": "echo env"}), (None, {"TUI_SHOW": "", "ITERM_SESSION_ID": "w0t0p0:A"})):
            with self.subTest(template=template, env=env):
                fake = Tmux()
                self.assertIsNone(self.show(fake, template, env))
                self.assertEqual(fake.calls, [])
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_failures_printed_and_returned(self):
        missing = FileNotFoundError(2, "No such file or directory", "/bin/sh")
        for template, result, why in (
                ("open {{name}} {{session}}", None, "unknown placeholder {{name}}"),
                ("false", (3,), "exit status 3"),
                ("sleep 99", subprocess.TimeoutExpired("/bin/sh", 30), "timed out after 30 s"),
                ("x", missing, str(missing))):
            with self.subTest(why=why):
                fake = Tmux(results={"/bin/sh": result} if result else {})
                self.assertEqual(self.show(fake, template), why)
                self.assertEqual(self.stderr.getvalue(), ATTACH + f"tui: show: {why}\n")
                self.assertEqual(len(fake.calls), 1 if result else 0)


class Iterm(unittest.TestCase):
    INSIDE = {"TMUX": "/tmp/tmux-501/default,1,0", "TMUX_PANE": "%3", "ITERM_SESSION_ID": "w0t0p0:ABC"}

    def show(self, fake, env, **kw):
        self.stderr = io.StringIO()
        with environ(**env), which(kw.pop("tmux", TMUX)), redirect_stderr(self.stderr):
            return tui.show("s", proc=fake, **kw)

    def test_anchor_by_iterm_session_id(self):
        for split in ("right", "below"):
            with self.subTest(split=split):
                fake = Tmux(results={"osascript": (0, "\n")})
                self.assertIsNone(self.show(fake, {"ITERM_SESSION_ID": "w0t0p0:ABC"}, split=split))
                self.assertEqual(fake.calls, [osa(split, "id", "s", "ABC")])
                self.assertEqual(fake.kwargs, [OSA_KW])
                self.assertEqual(self.stderr.getvalue(), ATTACH)
        fake, err = Tmux(results={"osascript": (0, "")}), io.StringIO()
        with environ(ITERM_SESSION_ID="w0t0p0:ABC"), which(), redirect_stderr(err):
            self.assertIsNone(tui.iterm("s", split="below", proc=fake))
        self.assertEqual((fake.calls, err.getvalue()), ([osa("below", "id", "s", "ABC")], ""))

    def test_anchor_by_own_session_inside_tmux(self):
        fake = Tmux(results={"display-message": (0, "own\n"), "osascript": (0, "\n"),
                             "list-clients": (0, "100 /dev/ttys001\n300 /dev/ttys003\n200 /dev/ttys002\n")})
        self.assertIsNone(self.show(fake, self.INSIDE))
        self.assertEqual(fake.calls, [["tmux", "display-message", "-p", "-t", "%3", "#{session_name}"],
                                      ["tmux", "list-clients", "-t", "=own", "-F", CLIENTS],
                                      osa("right", "tty", "s", "/dev/ttys003", "/dev/ttys002", "/dev/ttys001")])

    def test_detached_own_session_falls_back_to_iterm_session_id(self):
        fake = Tmux(results={"display-message": (0, "own\n"), "list-clients": (0, ""), "osascript": (0, "\n")})
        self.assertIsNone(self.show(fake, self.INSIDE))
        self.assertEqual(fake.calls[-1], osa("right", "id", "s", "ABC"))

    def test_anchor(self):
        own = {"display-message": (0, "own\n"), "list-clients": (0, "")}
        cases = [({"ITERM_SESSION_ID": "w0t0p0:ABC"}, {}, None, ("id", ["ABC"])),
                 (self.INSIDE, {**own, "list-clients": (0, "5 /dev/ttys004\n")}, None, ("tty", ["/dev/ttys004"])),
                 (self.INSIDE, own, None, ("id", ["ABC"])),
                 ({}, {"list-clients": (0, "5 /dev/ttys004\n")}, "b", ("tty", ["/dev/ttys004"]))]
        for env, results, beside, want in cases:
            with self.subTest(env=env, beside=beside), environ(**env):
                self.assertEqual(tui.anchor(beside, proc=Tmux(results=results)), want)
        no_iterm = {k: v for k, v in self.INSIDE.items() if k != "ITERM_SESSION_ID"}
        cases = [({}, {}, None, "no anchor pane"),
                 ({"ITERM_SESSION_ID": "w0t0p0"}, {}, None, "no anchor pane"),
                 (no_iterm, own, None, "no anchor pane: no terminal shows tmux session own"),
                 (self.INSIDE, {"list-clients": (0, "")}, "b", "^no terminal shows tmux session b$")]
        for env, results, beside, msg in cases:
            with self.subTest(env=env, beside=beside), environ(**env), self.assertRaisesRegex(tui.TuiError, msg):
                tui.anchor(beside, proc=Tmux(results=results))

    def test_own_session(self):
        fake = Tmux(results={"display-message": (0, "own\n")})
        with environ(**self.INSIDE):
            self.assertEqual(tui.own_session(proc=fake), "own")
        self.assertEqual(fake.calls, [["tmux", "display-message", "-p", "-t", "%3", "#{session_name}"]])
        fake = Tmux()
        with environ():
            self.assertIsNone(tui.own_session(proc=fake))
        self.assertEqual(fake.calls, [])
        with environ(**{**self.INSIDE, "TMUX_PANE": "%3;"}), self.assertRaisesRegex(tui.TuiError, "TMUX_PANE"):
            tui.own_session(proc=fake)

    def test_bad_tmux_pane(self):
        for env in ({**self.INSIDE, "TMUX_PANE": pane} for pane in ("", "3", "%3;", "%x", "%3 ", "#{x}")):
            with self.subTest(pane=env["TMUX_PANE"]):
                fake = Tmux()
                self.assertRegex(self.show(fake, env), r"TMUX_PANE")
                self.assertEqual(fake.calls, [])
        fake = Tmux()
        self.assertRegex(self.show(fake, {"TMUX": "x"}), r"TMUX_PANE")
        self.assertEqual(fake.calls, [])

    def test_own_session_name_checked(self):
        fake = Tmux(results={"display-message": (0, "a;\n")})
        self.assertRegex(self.show(fake, self.INSIDE), r"'a;'")
        self.assertEqual(fake.commands(), ["display-message"])

    def test_anchor_beside(self):
        fake = Tmux(results={"list-clients": (0, "5 /dev/ttys004\n"), "osascript": (0, "\n")})
        self.assertIsNone(self.show(fake, self.INSIDE, beside="b", split="below"))
        self.assertEqual(fake.calls, [["tmux", "list-clients", "-t", "=b", "-F", CLIENTS],
                                      osa("below", "tty", "s", "/dev/ttys004")])

    def test_beside_not_shown_anywhere(self):
        for result in ((0, ""), (1, "", "can't find session: =b\n")):
            with self.subTest(result=result):
                fake = Tmux(results={"list-clients": result})
                self.assertTrue(self.show(fake, {}, beside="b"))
                self.assertEqual(fake.commands(), ["list-clients"])

    def test_bad_arguments_raise(self):
        for kw in ({"beside": "a b"}, {"beside": "=b"}, {"split": "up"}, {"split": "vertically"}):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaises(tui.TuiError):
                    self.show(fake, {"ITERM_SESSION_ID": "w0t0p0:ABC"}, **kw)
                self.assertEqual(fake.calls, [])

    def test_template_ignores_split_and_beside(self):
        for template, env in (("open {{session}}", {}), (None, {"TUI_SHOW": "open {{session}}"})):
            with self.subTest(template=template):
                fake = Tmux()
                self.assertIsNone(self.show(fake, {**self.INSIDE, **env}, template=template, split="up",
                                            beside="a b"))
                self.assertEqual(fake.calls, [["/bin/sh", "-c", "open s"]])

    def test_no_anchor(self):
        for env in ({}, {"ITERM_SESSION_ID": "w0t0p0"}, {"ITERM_SESSION_ID": ""}):
            with self.subTest(env=env):
                fake = Tmux()
                self.assertTrue(self.show(fake, env))
                self.assertEqual(fake.calls, [])

    def test_no_tmux(self):
        fake = Tmux()
        self.assertEqual(self.show(fake, {"ITERM_SESSION_ID": "w0t0p0:ABC"}, tmux=None), "tmux not found")
        self.assertEqual(fake.calls, [])

    def test_tmux_path_made_absolute(self):
        here = os.getcwd()
        os.chdir(tempfile.gettempdir())
        self.addCleanup(os.chdir, here)
        fake = Tmux(results={"osascript": (0, "\n")})
        self.show(fake, {"ITERM_SESSION_ID": "w0t0p0:ABC"}, tmux="bin/tmux")
        self.assertEqual(fake.calls[0][5], os.path.join(os.getcwd(), "bin/tmux"))

    def test_osascript_failures(self):
        missing = FileNotFoundError(2, "No such file or directory", "osascript")
        for result, why in ((missing, f"osascript: {missing}"),
                            (subprocess.TimeoutExpired("osascript", 30), "osascript timed out after 30 s"),
                            ((1, "", "execution error: boom (-1708)\n"), "osascript: execution error: boom (-1708)"),
                            ((0, "no iTerm2 pane shows the anchor\n"), "no iTerm2 pane shows the anchor")):
            with self.subTest(why=why):
                fake = Tmux(results={"osascript": result})
                self.assertEqual(self.show(fake, {"ITERM_SESSION_ID": "w0t0p0:ABC"}), why)
                self.assertEqual(self.stderr.getvalue(), ATTACH + f"tui: show: {why}\n")

    def test_script_is_fixed_and_takes_its_values_as_arguments(self):
        script = tui.APPLESCRIPT
        self.assertNotIn(TMUX, script)
        for part in ("on run argv", 'if application "iTerm2" is running', 'tell application "iTerm2"',
                     "quoted form of tmuxPath", 'quoted form of ("=" & sessionName)',
                     "split vertically with default profile command", "split horizontally with default profile command",
                     "unique id of s", "tty of s"):
            self.assertIn(part, script)

    def test_panes_end_with_their_tmux_session(self):
        self.assertEqual(tui.APPLESCRIPT.count("with default profile command paneCommand"), 2)
        for word in ("write text", "close"):
            self.assertNotIn(word, tui.APPLESCRIPT)


class Cli(unittest.TestCase):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = tui.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def patch(self, name, **kw):
        p = unittest.mock.patch.object(tui, name, **kw)
        self.addCleanup(p.stop)
        return p.start()

    def test_start_forms(self):
        start = self.patch("start")
        for argv, session, command, template, split, beside in (
                ("start a -- cmd", "a", ["cmd"], None, "right", None),
                ("start b --beside a --split below -- cmd -x", "b", ["cmd", "-x"], None, "below", "a"),
                ("start --split below b --show T -- cmd", "b", ["cmd"], "T", "below", None),
                ("start a -- cmd -- x", "a", ["cmd", "--", "x"], None, "right", None),
                ("start b --beside a -- claude --x -- -p", "b", ["claude", "--x", "--", "-p"], None, "right", "a")):
            with self.subTest(argv=argv):
                start.reset_mock()
                self.assertEqual(self.main(*argv.split())[0], 0)
                start.assert_called_once_with(session, command, cwd=os.getcwd(), env=dict(os.environ),
                                              template=template, split=split, beside=beside)
        self.main("start", "--show", "", "a", "--", "cmd")
        self.assertEqual(start.call_args.kwargs["template"], "")

    def test_start_usage_errors(self):
        start = self.patch("start")
        for argv in ("start a", "start a --", "start a --split below cmd", "start a --bogus x -- cmd",
                     "start a=b -- cmd", "start --split up a -- cmd", "start b --beside a=b -- cmd", "start a cmd",
                     "start -- cmd", "start a b -- cmd", "start --show -- a -- cmd"):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv.split())[0], 2)
        for argv in ("start a", "start a cmd", "start a --split below cmd"):
            with self.subTest(argv=argv):
                self.assertIn("a command must follow --", self.main(*argv.split())[2])
        start.assert_not_called()

    def test_help(self):
        rc, out, _ = self.main("-h")
        self.assertEqual(rc, 0)
        self.assertIn("{start,send,read,show}", out)
        rc, out, _ = self.main("start", "-h")
        self.assertEqual(rc, 0)
        self.assertIn("session -- cmd", out)
        for option in ("--show", "--split", "--beside"):
            self.assertIn(option, out)

    def test_subcommands(self):
        send, read, show, status = (self.patch(n) for n in ("send", "read", "show", "status"))
        read.return_value, show.return_value, status.return_value = "pane\ntext", None, tui.RUNNING
        self.assertEqual(self.main("send", "s", "hi there"), (0, "", ""))
        send.assert_called_once_with("s", "hi there")
        self.assertEqual(self.main("send", "s", "--", "-x")[0], 0)
        self.assertEqual(send.call_args, unittest.mock.call("s", "-x"))
        self.assertEqual(self.main("read", "s"), (0, "pane\ntext\n", ""))
        self.assertEqual(self.main("read", "s", "--lines", "3")[0], 0)
        self.assertEqual(read.call_args_list, [unittest.mock.call("s", None), unittest.mock.call("s", 3)])
        self.assertEqual(self.main("show", "--show", "x", "--split", "below", "--beside", "b", "s"), (0, "", ""))
        status.assert_called_once_with("s")
        show.assert_called_once_with("s", "x", split="below", beside="b")
        self.main("show", "s")
        self.assertEqual(show.call_args, unittest.mock.call("s", None, split="right", beside=None))

    def test_exit_1(self):
        for name in ("start", "send", "read"):
            self.patch(name, side_effect=tui.TuiError("tmux: boom"))
        for argv in ("start a -- cmd", "send s hi", "read s"):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv.split()), (1, "", "tui: tmux: boom\n"))
        status, show = self.patch("status", return_value=None), self.patch("show", return_value="boom")
        self.assertEqual(self.main("show", "s"), (1, "", "tui: no session s\n"))
        show.assert_not_called()
        status.return_value = tui.RUNNING
        self.assertEqual(self.main("show", "s")[0], 1)

    def test_exit_2(self):
        called = [self.patch(n) for n in ("start", "send", "read", "show", "status")]
        for argv in ((), ("bogus",), ("send", "s"), ("send", "a b", "x"), ("read", "s", "--lines", "0"),
                     ("read", "s", "--lines", "x"), ("read", "s", "--lines", "-1"), ("show", "--split", "up", "s"),
                     ("show", "--beside", "a b", "s")):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv)[0], 2)
        for mock in called:
            mock.assert_not_called()


class SingleFile(unittest.TestCase):
    def setUp(self):
        with open(tui.__file__) as f:
            self.source = f.read()

    def test_imports_only_the_stdlib(self):
        names = set()
        for node in ast.walk(ast.parse(self.source)):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add("." * node.level + (node.module or "").split(".")[0])
        self.assertLessEqual(names, set(sys.stdlib_module_names))

    def test_python_3_9_syntax(self):
        ast.parse(self.source, feature_version=(3, 9))

    def test_runs_alone(self):
        with tempfile.TemporaryDirectory() as d:
            copy = shutil.copy(tui.__file__, d)
            res = subprocess.run([sys.executable, "-I", copy, "--help"], cwd=d, capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        for word in ("start", "send", "read", "show"):
            self.assertIn(word, res.stdout)


class Exec(unittest.TestCase):
    def test_safe_for_tmux(self):
        self.assertNotIn("#", tui.EXEC)
        self.assertFalse(tui.EXEC.rstrip().endswith(";"))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.path = os.path.join(self.root, "handover.json")

    def run_exec(self, argv, env, cwd, pane):
        """The wrapper as tmux runs it, given a handover file naming argv."""
        with open(self.path, "w") as f:
            json.dump({"argv": argv, "env": env, "cwd": cwd}, f)
        return subprocess.run([sys.executable, "-I", "-c", tui.EXEC, self.path], env=pane, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=30)

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
        self.assertEqual({k: got_env.get(k) for k in (*env, "TERM")}, {**env, "TERM": "tmux-256color"})
        self.assertNotIn("PANE_ONLY", got_env)
        self.assertNotIn("PATH", got_env)
        self.assertEqual(got_argv, ["-c", "x", "y z"])
        self.assertFalse(os.path.exists(self.path))

    def test_default_signal_dispositions(self):
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            with self.subTest(sig=sig.name):
                res = self.run_exec(["/bin/sh", "-c", f"kill -{sig.name[3:]} $$"], {}, self.root, {})
                self.assertEqual(res.returncode, -sig, res.stderr)


if __name__ == "__main__":
    unittest.main()
