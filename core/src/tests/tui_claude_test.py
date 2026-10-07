import ast
import io
import json
import os
import re
import shlex
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
import tui_claude  # noqa: E402

FORMAT = "#{pane_dead} #{pane_dead_status} #{pane_dead_signal}"
CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{socket_path}"
PGREP = ["pgrep", "-a", "-x", "iTerm2"]
ITERM = {"ITERM_SESSION_ID": "w0t0p0:ABC", "TERM_PROGRAM": "iTerm.app"}
TMUX_KW = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}
SH_KW = {"stdin": subprocess.DEVNULL, "timeout": 30}
OSA_KW = {**TMUX_KW, "timeout": 30}
TMUX = '/opt/a"b\\c/bin/tmux'   # the script text must never carry it
ATTACH = "tui: session s: tmux attach -t '=s'\n"
WATCH = "; watch it with tmux attach -t '=s'"
MATCHER = "permission_prompt|elicitation_dialog|agent_needs_input"
DIED = "set-option @state dead"
DIED_EVENTS = DIED + " ; run-shell -b 'echo \"$(date +%H:%M:%S)\" #{q:session_name} dead >> #{q:@events}'"
DECORATE = ["set-hook", "set-option", "set-option", "set-option"]
INSIDE = {"TMUX": "/tmp/tmux-501/default,1,0", "TMUX_PANE": "%3", "ITERM_SESSION_ID": "w0t0p0:ABC"}
OWN = ["tmux", "display-message", "-p", "-t", "%3", "#{session_name}"]
SESSIONS = ["tmux", "list-sessions", "-F", "#{session_id}\t#{session_name}\t#{@opener}\t#{@pane}\t#{socket_path}"]
LIVE = ["tmux", "list-panes", "-a", "-F", "#{pane_id}"]
HOSTS = ["tmux", "list-panes", "-a", "-F", "#{pane_dead} #{pane_tty} #{pane_id}"]
SOCK = "/tmp/s p"
# $TUI_ATTACH_PREFIX unset, blank and set, with the prefix each puts before the printed `tmux attach`
PREFIXES = (({}, ""), ({"TUI_ATTACH_PREFIX": " \t"}, ""),
            ({"TUI_ATTACH_PREFIX": " docker exec -it box "}, "docker exec -it box "))


def done(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


def record(opener, pane):
    """The set-options recording session s's opener (when known) and new pane."""
    return [*([["tmux", "set-option", "-t", "=s:", "@opener", opener]] if opener else []),
            ["tmux", "set-option", "-t", "=s:", "@pane", pane]]


def split_window(flag, pane, sock=SOCK):
    return ["tmux", "split-window", "-d", flag, "-P", "-F", "#{pane_id}", "-t", pane,
            "env", "-u", "TMUX", TMUX, "-S", sock, "attach", "-t", "=s"]


def clients_of(session):
    return ["tmux", "list-clients", "-t", f"={session}", "-F", CLIENTS]


def environ(**values):
    return unittest.mock.patch.dict(os.environ, values, clear=True)


def which(tmux=TMUX):
    real = shutil.which
    return unittest.mock.patch("shutil.which", side_effect=lambda cmd, *a, **kw: tmux if cmd == "tmux"
                               else real(cmd, *a, **kw))


def osa(split, kind, session, *anchors):
    return ["osascript", "-e", tui_claude.APPLESCRIPT, split, kind, TMUX, session, *anchors]


def decorations(session, events=None, status="off"):
    target = f"={session}:"
    return [*([["tmux", "set-option", "-t", target, "@events", events]] if events else []),
            ["tmux", "set-hook", "-p", "-t", target, "pane-died", DIED_EVENTS if events else DIED],
            ["tmux", "set-option", "-w", "-t", target, "pane-border-status", "top"],
            ["tmux", "set-option", "-w", "-t", target, "pane-border-format", " #{session_name} #{@state} "],
            ["tmux", "set-option", "-t", target, "status", status]]


def panes(*rows):
    """A list-panes result printing rows of (pane_dead, pane_tty, pane_id) in the call's -F format."""
    def result(argv):
        fmt = argv[argv.index("-F") + 1]
        values = [dict(zip(("pane_dead", "pane_tty", "pane_id"), row)) for row in rows]
        return 0, "".join(re.sub(r"#\{(\w+)\}", lambda m: v[m.group(1)], fmt) + "\n" for v in values)
    return result


class Tmux:
    """Plays tmux, and sh and osascript: records each call. `results` maps a tmux command or another program to its
    (rc, stdout, stderr), an exception to raise, or a function of the argv giving either. `pane` is what display-message
    prints (None: no server). On new-session it reads the handover file named in its argv, then unlinks it as the
    wrapper would, unless `hand_over` is False."""

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
            if callable(result):
                result = result(argv)
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
                  unittest.mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("TUI_ATTACH_PREFIX", None)
        self.sleep = unittest.mock.Mock()
        self.stderr = io.StringIO()

    def start(self, fake, session="s", argv=("tool", "-x", "a b"), env=None, template="", **kw):
        env = {"PATH": self.bin, "HOME": "/h"} if env is None else env
        with redirect_stderr(self.stderr):
            tui_claude.start(session, list(argv), cwd=self.cwd, env=env, template=template, proc=fake, sleep=self.sleep, **kw)

    def test_new_session_runs_the_wrapper_keeps_a_dead_pane_then_decorates(self):
        fake = Tmux()
        self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", *DECORATE])
        self.assertEqual(fake.calls[1], ["tmux", "new-session", "-d", "-s", "s", "-x", "120", "-y", "40",
                                         sys.executable, "-I", "-c", tui_claude.EXEC, fake.path,
                                         ";", "set-option", "-p", "-t", "=s:", "remain-on-exit", "on"])
        self.assertNotIn("-c", fake.calls[1][:fake.calls[1].index(sys.executable)])
        self.assertEqual([a for a in fake.calls[1] if a.endswith(";")], [";"])
        self.assertEqual(fake.kwargs, [TMUX_KW] * len(fake.calls))
        self.sleep.assert_not_called()
        self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_handover_file(self):
        env = {"PATH": self.bin, "HOME": "/h", "CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1",
               **{k: "x" for k in tui_claude.TERMINAL_KEYS}}
        fake = Tmux()
        self.start(fake, env=env)
        self.assertEqual(fake.handover, {"argv": [self.tool, "--settings", tui_claude.hooks(), "-x", "a b"],
                                         "env": {"PATH": self.bin, "HOME": "/h", "CLAUDECODE": "1"}, "cwd": self.cwd})
        self.assertEqual(fake.mode, 0o600)
        self.assertEqual(fake.dir_mode, 0o700)
        self.assertEqual(os.path.dirname(os.path.dirname(fake.path)), self.temp)
        self.assertEqual(os.listdir(self.temp), [])

    def test_terminal_keys(self):
        self.assertEqual(set(tui_claude.TERMINAL_KEYS), {
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
        with self.assertRaisesRegex(tui_claude.TuiError, "sh"):
            self.start(fake, argv=("sh", "-c", "true"))
        with self.assertRaisesRegex(tui_claude.TuiError, "no-such-tool"):
            self.start(fake, argv=("no-such-tool",))
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_empty_argv(self):
        fake = Tmux()
        with self.assertRaisesRegex(tui_claude.TuiError, "no command"):
            self.start(fake, argv=())
        self.assertEqual(fake.calls, [])

    def test_handover_file_unwritable(self):
        fake = Tmux()
        with unittest.mock.patch.object(tempfile, "tempdir", os.path.join(self.root, "missing")), \
                self.assertRaisesRegex(tui_claude.TuiError, "^handover: "):
            self.start(fake)
        real = os.open

        def refuse(path, *args, **kw):
            if str(path).endswith("handover.json"):
                raise PermissionError(13, "Permission denied", path)
            return real(path, *args, **kw)
        with unittest.mock.patch("os.open", side_effect=refuse), self.assertRaisesRegex(tui_claude.TuiError, "^handover: "):
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
            self.assertEqual(fake.handover["argv"], [self.tool, "--settings", tui_claude.hooks()])

    def test_live_session_is_refused(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env), unittest.mock.patch.dict(os.environ, env):
                fake = Tmux(pane="0  ")
                with self.assertRaisesRegex(tui_claude.TuiError,
                                            "^" + re.escape(f"session s is running; {prefix}tmux attach -t '=s'") + "$"):
                    self.start(fake)
                self.assertEqual(fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]])
                self.assertEqual(os.listdir(self.temp), [])

    def test_dead_session_is_killed_then_started(self):
        fake = Tmux(pane="1 0 ")
        self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "kill-session", "new-session", *DECORATE])
        self.assertEqual(fake.calls[1], ["tmux", "kill-session", "-t", "=s"])

    def test_a_failed_tmux_call_kills_the_session_it_may_have_made(self):
        fake = Tmux(fail={"new-session"})   # e.g. the chained set-option failed after new-session made the session
        with self.assertRaisesRegex(tui_claude.TuiError, r"^tmux: boom$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
        self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
        self.assertEqual(os.listdir(self.temp), [])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_a_duplicate_session_is_not_killed(self):
        fake = Tmux(results={"new-session": (1, "", "duplicate session: s\n")})   # another start took the name
        with self.assertRaisesRegex(tui_claude.TuiError, r"^tmux: duplicate session: s$"):
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
                with unittest.mock.patch.object(tui_claude, "show", side_effect=interrupt), \
                        self.assertRaises(KeyboardInterrupt):
                    self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session",
                                                   *(DECORATE if where == "show" else []), "kill-session"])
                self.assertIs(fake.dir_at_kill, False)
                self.assertEqual(os.listdir(self.temp), [])

    def test_tmux_missing(self):
        def missing(argv, **kw):
            raise FileNotFoundError(2, "No such file or directory", "tmux")
        with self.assertRaisesRegex(tui_claude.TuiError, r"^tmux: "):
            self.start(missing)
        self.assertEqual(os.listdir(self.temp), [])

    def test_wrapper_never_reads(self):
        for fail in ((), {"kill-session"}):
            with self.subTest(fail=fail):
                self.sleep.reset_mock()
                fake = Tmux(fail=fail, hand_over=False)
                with self.assertRaisesRegex(tui_claude.TuiError, r"^the session did not start$"):
                    self.start(fake)
                self.assertEqual(fake.commands(), ["display-message", "new-session", "kill-session"])
                self.assertEqual(fake.calls[2], ["tmux", "kill-session", "-t", "=s"])
                self.assertIs(fake.dir_at_kill, False)
                self.assertAlmostEqual(sum(c.args[0] for c in self.sleep.call_args_list), tui_claude.HANDOVER_TIMEOUT)
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
        self.assertEqual(fake.commands(), ["display-message", "new-session", *DECORATE])
        self.assertEqual(os.listdir(self.temp), [])

    def test_unsafe_tmux_argument(self):
        for exe in ("/opt/py#3/bin/python3", "/opt/bin/python3;"):
            with self.subTest(exe=exe), unittest.mock.patch.object(sys, "executable", exe):
                fake = Tmux()
                with self.assertRaises(tui_claude.TuiError):
                    self.start(fake)
                self.assertNotIn("new-session", fake.commands())
        hashed = os.path.join(self.root, "t#mp")
        os.makedirs(hashed)
        fake = Tmux()
        with unittest.mock.patch.object(tempfile, "tempdir", hashed), self.assertRaises(tui_claude.TuiError):
            self.start(fake)
        self.assertNotIn("new-session", fake.commands())
        self.assertEqual(os.listdir(hashed), [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_invalid_session_name(self):
        for name in ("", "a b", "a.b", "a:b", "=a", "#{x}", "a;", "a\n", "é"):
            with self.subTest(name=name):
                fake = Tmux()
                for call in (lambda: self.start(fake, session=name), lambda: tui_claude.status(name, proc=fake),
                             lambda: tui_claude.kill(name, proc=fake), lambda: tui_claude.send(name, "x", proc=fake, sleep=self.sleep),
                             lambda: tui_claude.read(name, proc=fake)):
                    with self.assertRaises(tui_claude.TuiError):
                        call()
                self.assertEqual(fake.calls, [])

    def test_show_gets_its_arguments_after_the_handover(self):
        fake = Tmux()
        seen = []

        def show(*args, **kw):
            seen.append((args, kw, os.path.exists(fake.path), fake.commands()))
        with unittest.mock.patch.object(tui_claude, "show", side_effect=show):
            self.start(fake, template="tmpl", split="below", split_from="b", opener="o")
        self.assertEqual(seen, [(("s", "tmpl"), {"split": "below", "split_from": "b", "opener": "o", "proc": fake}, False,
                                 ["display-message", "new-session", *DECORATE])])

    def test_a_failed_show_is_only_printed(self):
        fake = Tmux(results={"osascript": FileNotFoundError(2, "No such file or directory", "osascript")})
        with environ(**ITERM), which():
            self.start(fake, template=None)
        self.assertEqual(fake.commands(), ["display-message", "new-session", *DECORATE, "list-sessions", "pgrep",
                                           "osascript"])
        self.assertEqual(self.stderr.getvalue(),
                         ATTACH + f"tui: show: osascript: [Errno 2] No such file or directory: 'osascript'{WATCH}\n")

    def test_in_a_container_the_show_is_a_tmux_split(self):
        fake = Container.fake(Container.PGREPS[-1])
        with environ(**Container.ENV), which():
            self.start(fake, template=None)
        self.assertEqual(fake.commands(), ["display-message", "new-session", *DECORATE, "display-message",
                                           "list-sessions", "list-clients", "list-panes", "pgrep", "split-window",
                                           "set-option", "set-option"])
        self.assertEqual(fake.calls[-3], split_window("-h", "%0", Container.SOCK))
        self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_bad_layout_refused_before_tmux(self):
        with environ(**ITERM), which():
            for kw in ({"split": "up"}, {"split_from": "a b"}, {"opener": "a:b:c"}):
                with self.subTest(**kw):
                    fake = Tmux()
                    with self.assertRaises(tui_claude.TuiError):
                        self.start(fake, template=None, **kw)
                    self.assertEqual(fake.calls, [])
            fake = Tmux()
            self.start(fake, template="", split="up", split_from="a b", opener="a:b:c")
            self.assertEqual(fake.commands(), ["display-message", "new-session", *DECORATE])

    def test_hooks_events_and_decorations(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        events = os.path.join(self.root, "ev")
        for given, path in ((None, None), ("ev", events)):
            with self.subTest(events=given):
                fake = Tmux()
                self.start(fake, events=given)
                self.assertEqual(fake.handover["argv"], [self.tool, "--settings", tui_claude.hooks(path), "-x", "a b"])
                self.assertEqual(fake.calls[2:], decorations("s", path))
        self.assertEqual(stat.S_IMODE(os.stat(events).st_mode), 0o600)

    def test_status_line_is_the_callers_choice(self):
        for status_line, status in ((False, "off"), (True, "on")):
            with self.subTest(status_line=status_line):
                fake = Tmux()
                self.start(fake, status_line=status_line)
                self.assertEqual(fake.calls[2:], decorations("s", status=status))

    def test_a_failed_decoration_kills_the_session(self):
        fake = Tmux(fail={"set-hook"})
        with self.assertRaisesRegex(tui_claude.TuiError, r"^tmux: boom$"):
            self.start(fake)
        self.assertEqual(fake.commands(), ["display-message", "new-session", "set-hook", "kill-session"])
        self.assertEqual(fake.calls[-1], ["tmux", "kill-session", "-t", "=s"])
        self.assertEqual(os.listdir(self.temp), [])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_bad_events_file_or_settings_refused_before_tmux(self):
        link = os.path.join(self.root, "link")
        os.symlink(self.tool, link)
        for kw in ({"events": self.cwd}, {"events": link}, {"argv": ("tool", "--settings", "s.json")}):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaises(tui_claude.TuiError):
                    self.start(fake, **kw)
                self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])


class Hooks(unittest.TestCase):
    """Runs each hook's command with sh and a fake tmux on PATH that logs its arguments and prints w1 (#S)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        bin_dir = os.path.join(self.root, "bin")
        os.makedirs(bin_dir)
        self.log = os.path.join(self.root, "tmux.log")
        with open(os.path.join(bin_dir, "tmux"), "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> {shlex.quote(self.log)}\necho w1\n')
        os.chmod(os.path.join(bin_dir, "tmux"), 0o755)
        self.path = f"{bin_dir}:/usr/bin:/bin"

    def run_hook(self, events, event, pane="%1"):
        """The hook's sh result and the fake tmux's calls."""
        cmd = json.loads(tui_claude.hooks(events))["hooks"][event][0]["hooks"][0]["command"]
        env = {"PATH": self.path, **({"TMUX_PANE": pane} if pane else {})}
        res = subprocess.run(["/bin/sh", "-c", cmd], env=env, cwd=self.root, capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=30)
        calls = []
        if os.path.exists(self.log):
            with open(self.log) as f:
                calls = f.read().splitlines()
            os.unlink(self.log)
        return res, calls

    def test_shape(self):
        for events in (None, "/e"):
            with self.subTest(events=events):
                hooks = json.loads(tui_claude.hooks(events))
                self.assertEqual(list(hooks), ["hooks"])
                self.assertEqual({k: [set(e) for e in v] for k, v in hooks["hooks"].items()},
                                 {"Stop": [{"hooks"}], "Notification": [{"matcher", "hooks"}],
                                  "UserPromptSubmit": [{"hooks"}]})
                self.assertEqual(hooks["hooks"]["Notification"][0]["matcher"], MATCHER)
                for entries in hooks["hooks"].values():
                    self.assertEqual([h["type"] for h in entries[0]["hooks"]], ["command"])

    def test_state_without_events(self):
        for event, word in (("Stop", "done"), ("Notification", "blocked"), ("UserPromptSubmit", "working")):
            with self.subTest(event=event):
                res, calls = self.run_hook(None, event)
                self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
                self.assertEqual(calls, [f"set-option -t %1 @state {word}"])
        self.assertEqual(os.listdir(self.root), ["bin"])

    def test_state_and_event_lines_with_events(self):
        for sub in ("a b", "it's", "$(touch x)"):
            events = os.path.join(self.root, sub, "e")
            os.makedirs(os.path.dirname(events))
            for event, word in (("Stop", "done"), ("Notification", "blocked"), ("UserPromptSubmit", "working")):
                with self.subTest(sub=sub, event=event):
                    res, calls = self.run_hook(events, event)
                    self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
                    self.assertEqual(calls[0], f"set-option -t %1 @state {word}")
            with open(events) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
            for line, word in zip(lines, ("done", "blocked")):
                self.assertRegex(line, rf"^\d\d:\d\d:\d\d w1 {word}\n$")
        self.assertFalse(os.path.exists(os.path.join(self.root, "x")))

    def test_outside_tmux_a_no_op(self):
        events = os.path.join(self.root, "e")
        for event in ("Stop", "Notification", "UserPromptSubmit"):
            with self.subTest(event=event):
                res, calls = self.run_hook(events, event, pane=None)
                self.assertEqual((res.returncode, calls), (0, []))
        self.assertFalse(os.path.exists(events))


class WithHooks(unittest.TestCase):
    STOP = {"hooks": [{"type": "command", "command": "report.py stop"}]}

    def test_added_after_the_command(self):
        for events in (None, "/e"):
            with self.subTest(events=events):
                rest = ["-p", "x", "--", "--settings", "{}"]
                argv = ["claude", *rest]
                want = ["claude", "--settings", tui_claude.hooks(events), *rest]
                self.assertEqual(tui_claude.with_hooks(argv, events), want)
                self.assertEqual(argv, ["claude", *rest])

    def test_merged_into_settings(self):
        ours = json.loads(tui_claude.hooks("/e"))["hooks"]
        pre = [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]
        for settings, want in (({"model": "m", "hooks": {"Stop": [self.STOP], "PreToolUse": pre}},
                                {"model": "m", "hooks": {"Stop": [self.STOP, *ours["Stop"]], "PreToolUse": pre,
                                                         "Notification": ours["Notification"],
                                                         "UserPromptSubmit": ours["UserPromptSubmit"]}}),
                               ({"model": "m"}, {"model": "m", "hooks": ours})):
            value = json.dumps(settings)
            for inline in (False, True):
                with self.subTest(settings=settings, inline=inline):
                    flag = [f"--settings={value}"] if inline else ["--settings", value]
                    argv = ["claude", "hi", "--allowedTools", "Bash(x)", *flag, "--settings", "second"]
                    got = tui_claude.with_hooks(argv, "/e")
                    i, prefix = (4, "--settings=") if inline else (5, "")
                    self.assertEqual(got[:i] + got[i + 1:], argv[:i] + argv[i + 1:])
                    self.assertTrue(got[i].startswith(prefix))
                    self.assertEqual(json.loads(got[i][len(prefix):]), want)
                    self.assertEqual(argv[4:], [*flag, "--settings", "second"])

    def test_refuses_a_value_it_cannot_merge_into(self):
        for argv in (["claude", "--settings", "s.json"], ["claude", "x", "--settings=[]"],
                     ["claude", "--settings", "{"], ["claude", "--settings"], ["claude", "--settings", '{"hooks": []}'],
                     ["claude", "--settings", '{"hooks": {"Stop": {}}}']):
            with self.subTest(argv=argv), self.assertRaisesRegex(tui_claude.TuiError, "--settings"):
                tui_claude.with_hooks(argv)
        with self.assertRaisesRegex(tui_claude.TuiError, "no command"):
            tui_claude.with_hooks([])


class EventsFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)

    def test_created_0600_and_made_absolute(self):
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        path = os.path.join(self.root, "e")
        self.assertEqual(tui_claude.events_file("e"), path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, "a") as f:
            f.write("x\n")
        self.assertEqual(tui_claude.events_file(path), path)
        with open(path) as f:
            self.assertEqual(f.read(), "x\n")

    def test_refused(self):
        target = os.path.join(self.root, "target")
        open(target, "w").close()
        link = os.path.join(self.root, "link")
        os.symlink(target, link)
        for path in (link, self.root, os.path.join(self.root, "missing", "e")):
            with self.subTest(path=path), self.assertRaisesRegex(tui_claude.TuiError, f"^events file {re.escape(path)}: "):
                tui_claude.events_file(path)
        fifo = unittest.mock.Mock(st_mode=stat.S_IFIFO | 0o600, st_uid=os.getuid())
        for patch in (unittest.mock.patch("os.getuid", return_value=os.getuid() + 1),
                      unittest.mock.patch("os.fstat", return_value=fifo)):
            with self.subTest(patch=patch), patch, self.assertRaisesRegex(tui_claude.TuiError, "owned by you"):
                tui_claude.events_file(target)

    def test_refuses_a_path_tmux_would_misread(self):
        for name in ("a#b", "e;"):
            path = os.path.join(self.root, name)
            with self.subTest(name=name), \
                    self.assertRaisesRegex(tui_claude.TuiError, f"^events file {re.escape(path)}: tmux would misread"):
                tui_claude.events_file(path)
            self.assertFalse(os.path.exists(path))


class Decorate(unittest.TestCase):
    def test_tmux_calls(self):
        for events in (None, "/a b/e"):
            with self.subTest(events=events):
                fake = Tmux()
                tui_claude.decorate("s", events, proc=fake)
                self.assertEqual(fake.calls, decorations("s", events))
                self.assertEqual(fake.kwargs, [TMUX_KW] * len(fake.calls))
        fake = Tmux()
        tui_claude.decorate("s", status_line=True, proc=fake)
        self.assertEqual(fake.calls, decorations("s", status="on"))

    def test_failure(self):
        for events, step in ((None, "set-hook"), ("/e", "set-option")):
            with self.subTest(events=events):
                fake = Tmux(fail={step})
                with self.assertRaisesRegex(tui_claude.TuiError, r"^tmux: boom$"):
                    tui_claude.decorate("s", events, proc=fake)
                self.assertEqual(len(fake.calls), 1)

    def test_invalid_session_name(self):
        fake = Tmux()
        with self.assertRaises(tui_claude.TuiError):
            tui_claude.decorate("a b", proc=fake)
        self.assertEqual(fake.calls, [])


class Status(unittest.TestCase):
    def test_states(self):
        for pane, want in (("0  ", tui_claude.RUNNING), ("1 3 ", 3), ("1 0 ", 0), ("1  kill", 137), ("1  9", 137),
                           ("1  ", tui_claude.RUNNING), ("  ", None), (None, None)):
            with self.subTest(pane=pane):
                fake = Tmux(pane=pane)
                self.assertEqual(tui_claude.status("s", proc=fake), want)
                self.assertEqual(fake.calls, [["tmux", "display-message", "-p", "-t", "=s:", FORMAT]])
                self.assertEqual(fake.kwargs, [TMUX_KW])


class Kill(unittest.TestCase):
    def test_kill(self):
        fake = Tmux()
        tui_claude.kill("s", proc=fake)
        self.assertEqual(fake.calls, [["tmux", "kill-session", "-t", "=s"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_failure(self):
        with self.assertRaisesRegex(tui_claude.TuiError, r"^tmux: boom$"):
            tui_claude.kill("s", proc=Tmux(fail={"kill-session"}))


class Send(unittest.TestCase):
    def send(self, fake, text):
        tui_claude.send("s", text, proc=fake, sleep=lambda seconds: fake.calls.append(("sleep", seconds)))
        buffer = fake.calls[0][3]
        self.assertRegex(buffer, r"^tui-s-[0-9a-f]{32}$")
        return buffer

    def test_pastes_the_text_then_presses_enter(self):
        fake = Tmux()
        buffer = self.send(fake, "-x hi")
        self.assertEqual(fake.calls, [["tmux", "load-buffer", "-b", buffer, "-"],
                                      ["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", "=s:"], ("sleep", 0.5),
                                      ["tmux", "send-keys", "-t", "=s:", "Enter"]])
        self.assertEqual(fake.kwargs, [{"capture_output": True, "text": True, "input": "-x hi"}, TMUX_KW, TMUX_KW])
        self.assertEqual(tui_claude.PAUSE, 0.5)

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
        for step, calls in (("load-buffer", 1), ("paste-buffer", 2)):
            with self.subTest(step=step):
                fake, sleep = Tmux(fail={step}), unittest.mock.Mock()
                with self.assertRaisesRegex(tui_claude.TuiError, r"^tmux: boom$"):
                    tui_claude.send("s", "hi", proc=fake, sleep=sleep)
                self.assertEqual(len(fake.calls), calls)
                sleep.assert_not_called()


class Read(unittest.TestCase):
    def test_visible_pane(self):
        fake = Tmux(results={"capture-pane": (0, "a\n  b\n\n \n")})
        self.assertEqual(tui_claude.read("s", proc=fake), "a\n  b")
        self.assertEqual(fake.calls, [["tmux", "capture-pane", "-p", "-J", "-t", "=s:"]])
        self.assertEqual(fake.kwargs, [TMUX_KW])

    def test_last_lines(self):
        fake = Tmux(results={"capture-pane": (0, "1\n2\n\n3\n4\n\n\n")})
        self.assertEqual(tui_claude.read("s", 3, proc=fake), "\n3\n4")
        self.assertEqual(fake.calls, [["tmux", "capture-pane", "-p", "-J", "-t", "=s:", "-S", "-3"]])

    def test_lines_must_be_a_positive_int(self):
        fake = Tmux()
        for lines in (0, -1, True, 1.5, "3"):
            with self.subTest(lines=lines), self.assertRaises(tui_claude.TuiError):
                tui_claude.read("s", lines, proc=fake)
        self.assertEqual(fake.calls, [])

    def test_failure(self):
        with self.assertRaisesRegex(tui_claude.TuiError, r"^tmux: boom$"):
            tui_claude.read("s", proc=Tmux(fail={"capture-pane"}))


class Show(unittest.TestCase):
    def show(self, fake, template=None, env=None, session="s", **kw):
        self.stderr = io.StringIO()
        with environ(**(env or {})), which(), redirect_stderr(self.stderr):
            return tui_claude.show(session, template, proc=fake, **kw)

    def test_constants(self):
        self.assertEqual((tui_claude.SHOW_TIMEOUT, tui_claude.PLACEHOLDERS), (30, {"session"}))

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
                self.assertEqual(self.stderr.getvalue(), f"tui: session s: {prefix}tmux attach -t '=s'\n")

    def test_session_quoted(self):
        fake = Tmux()
        self.show(fake, "open {{session}}", session="a;b c")
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open 'a;b c'"]])

    def test_template_else_the_split(self):
        split = [SESSIONS, PGREP, osa("right", "id", "s", "ABC"), *record("w0t0p0:ABC", "NEW")]
        for template, env, want in (("echo arg", ITERM, [["/bin/sh", "-c", "echo arg"]]),
                                    (None, ITERM, split)):
            with self.subTest(template=template, env=env):
                fake = Tmux(results={"osascript": (0, "ok NEW\n")})
                self.assertIsNone(self.show(fake, template, env))
                self.assertEqual(fake.calls, want)
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_empty_runs_nothing(self):
        fake = Tmux()
        self.assertIsNone(self.show(fake, "", ITERM))
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


class Pane(unittest.TestCase):
    def show(self, fake, env, **kw):
        self.stderr = io.StringIO()
        with environ(**env), which(kw.pop("tmux", TMUX)), redirect_stderr(self.stderr):
            return tui_claude.show("s", proc=fake, **kw)

    def clients(self, *rows):
        return 0, "".join(f"{at} {tty} {pane} {SOCK}\n" for at, tty, pane in rows)

    def test_anchor_by_iterm_session_id(self):
        for split in ("right", "below"):
            with self.subTest(split=split):
                fake = Tmux(results={"osascript": (0, "ok NEW\n")})
                self.assertIsNone(self.show(fake, ITERM, split=split))
                self.assertEqual(fake.calls, [PGREP, osa(split, "id", "s", "ABC"), *record("w0t0p0:ABC", "NEW")])
                self.assertEqual(fake.kwargs, [TMUX_KW, OSA_KW, TMUX_KW, TMUX_KW])
                self.assertEqual(self.stderr.getvalue(), ATTACH)
        fake, err = Tmux(results={"osascript": (0, "ok NEW")}), io.StringIO()
        with environ(**ITERM), which(), redirect_stderr(err):
            self.assertIsNone(tui_claude.open_pane("s", split="below", proc=fake))
        self.assertEqual((fake.calls[:2], err.getvalue()), ([PGREP, osa("below", "id", "s", "ABC")], ""))

    def test_iterm2_pane_showing_own_session(self):
        fake = Tmux(results={"display-message": (0, "own\n"), "osascript": (0, "ok NEW\n"),
                             "list-clients": self.clients((100, "/dev/ttys001", "%1"), (300, "/dev/ttys003", "%2"),
                                                          (200, "/dev/ttys002", "%9"))})
        self.assertIsNone(self.show(fake, INSIDE))
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("own"), HOSTS, PGREP,
                                      osa("right", "tty", "s", "/dev/ttys003", "/dev/ttys002", "/dev/ttys001"),
                                      *record("own", "NEW")])

    def test_tmux_split_of_own_pane_when_no_iterm2_pane_shows_it(self):
        clients = self.clients((5, "/dev/ttys004", "%9"))
        for split, flag in (("right", "-h"), ("below", "-v")):
            for not_iterm in ({"pgrep": (1,)}, {"pgrep": FileNotFoundError(2, "No such file", "pgrep")},
                              {"osascript": (0, "no iTerm2 pane shows the anchor\n")},
                              {"osascript": (1, "", "syntax error\n")}):
                with self.subTest(split=split, not_iterm=not_iterm):
                    fake = Tmux(results={"display-message": (0, "own\n"), "list-clients": clients,
                                         "split-window": (0, "%10\n"), **not_iterm})
                    self.assertIsNone(self.show(fake, INSIDE, split=split))
                    self.assertEqual(fake.calls[-3:], [split_window(flag, "%3"), *record("own", "%10")])
                    self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_detached_own_session_falls_back_to_iterm_session_id(self):
        fake = Tmux(results={"display-message": (0, "own\n"), "list-clients": (0, ""), "osascript": (0, "ok NEW\n")})
        self.assertIsNone(self.show(fake, INSIDE))
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("own"), PGREP, osa("right", "id", "s", "ABC"),
                                      *record("own", "NEW")])

    def test_anchor(self):
        own = {"display-message": (0, "own\n"), "list-clients": (0, "")}
        shown = self.clients((5, "/dev/ttys004", "%9"))
        nested = panes(("0", "/dev/ttys001", "%1"), ("0", "/dev/ttys004", "%5"))
        cases = [(ITERM, {}, None, tui_claude.Anchor(("id", ["ABC"]))),
                 (INSIDE, {**own, "list-clients": shown}, None,
                  tui_claude.Anchor(("tty", ["/dev/ttys004"]), "%3", SOCK, "own")),
                 (INSIDE, {**own, "list-clients": shown, "list-panes": nested}, None,
                  tui_claude.Anchor(None, "%5", SOCK, "own")),
                 (INSIDE, own, None, tui_claude.Anchor(("id", ["ABC"]))),
                 ({}, {"list-clients": shown}, "b", tui_claude.Anchor(("tty", ["/dev/ttys004"]), "%9", SOCK, "b")),
                 ({}, {"list-clients": shown, "list-panes": nested}, "b", tui_claude.Anchor(None, "%5", SOCK, "b")),
                 ({}, {"list-clients": (0, "5 /dev/ttys004\n7 /dev/ttys005 junk /s\n" + shown[1])}, "b",
                  tui_claude.Anchor(("tty", ["/dev/ttys004"]), "%9", SOCK, "b"))]
        for env, results, split_from, want in cases:
            with self.subTest(env=env, results=results, split_from=split_from), environ(**env):
                self.assertEqual(tui_claude.anchor(split_from, proc=Tmux(results=results)), want)
        no_iterm = {k: v for k, v in INSIDE.items() if k != "ITERM_SESSION_ID"}
        cases = [({}, {}, None, "no anchor pane: not in tmux"),
                 ({"ITERM_SESSION_ID": "w0t0p0"}, {}, None, "no anchor pane"),
                 ({"ITERM_SESSION_ID": "w0t0p0:ABC"}, {}, None, "no anchor pane"),
                 ({"ITERM_SESSION_ID": "w0t0p0:ABC", "TERM_PROGRAM": "vscode"}, {}, None, "no anchor pane"),
                 (no_iterm, own, None, "no anchor pane: no terminal shows tmux session own"),
                 (INSIDE, {"list-clients": (0, "")}, "b", "^no terminal shows tmux session b$")]
        for env, results, split_from, msg in cases:
            with self.subTest(env=env, split_from=split_from), environ(**env), self.assertRaisesRegex(tui_claude.TuiError, msg):
                tui_claude.anchor(split_from, proc=Tmux(results=results))

    def test_a_dead_pane_sharing_the_clients_tty_is_skipped(self):
        # a dead worker pane keeps its tty, which a later terminal may get
        shown = self.clients((5, "/dev/ttys004", "%9"))
        live, dead = ("0", "/dev/ttys004", "%5"), ("1", "/dev/ttys004", "%6")
        for rows, want in (([live, dead], tui_claude.Anchor(None, "%5", SOCK, "b")),
                           ([dead], tui_claude.Anchor(("tty", ["/dev/ttys004"]), "%9", SOCK, "b"))):
            with self.subTest(rows=rows), environ():
                fake = Tmux(results={"list-clients": shown, "list-panes": panes(*rows)})
                self.assertEqual(tui_claude.anchor("b", proc=fake), want)

    def test_own_session(self):
        fake = Tmux(results={"display-message": (0, "own\n")})
        with environ(**INSIDE):
            self.assertEqual(tui_claude.own_session(proc=fake), "own")
        self.assertEqual(fake.calls, [OWN])
        fake = Tmux()
        with environ():
            self.assertIsNone(tui_claude.own_session(proc=fake))
        self.assertEqual(fake.calls, [])
        with environ(**{**INSIDE, "TMUX_PANE": "%3;"}), self.assertRaisesRegex(tui_claude.TuiError, "TMUX_PANE"):
            tui_claude.own_session(proc=fake)

    def test_bad_tmux_pane(self):
        for env in ({**INSIDE, "TMUX_PANE": pane} for pane in ("", "3", "%3;", "%x", "%3 ", "#{x}")):
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

    def test_iterm2_pane_split_from(self):
        fake = Tmux(results={"display-message": (0, "own\n"), "list-clients": self.clients((5, "/dev/ttys004", "%9")),
                             "osascript": (0, "ok NEW\n")})
        self.assertIsNone(self.show(fake, INSIDE, split_from="b", split="below"))
        self.assertEqual(fake.calls, [OWN, clients_of("b"), HOSTS, PGREP, osa("below", "tty", "s", "/dev/ttys004"),
                                      *record("own", "NEW")])

    def test_tmux_split_split_from(self):
        clients = self.clients((5, "/dev/ttys004", "%9"), (9, "/dev/ttys005", "%7"))
        fake = Tmux(results={"list-clients": clients, "pgrep": (1,), "split-window": (0, "%10\n")})
        self.assertIsNone(self.show(fake, {}, split_from="b", split="below"))
        self.assertEqual(fake.calls[-2:], [split_window("-v", "%7"), *record(None, "%10")])
        nested = Tmux(results={"list-clients": clients, "list-panes": panes(("0", "/dev/ttys005", "%5")),
                               "split-window": (0, "%10\n")})
        self.assertIsNone(self.show(nested, {}, split_from="b"))
        self.assertEqual(nested.calls, [clients_of("b"), HOSTS, split_window("-h", "%5"), *record(None, "%10")])

    def test_tmux_split_failures(self):
        clients = self.clients((5, "/dev/ttys004", "%9"))
        fake = Tmux(results={"list-clients": clients, "pgrep": (1,), "split-window": (1, "", "no space for new pane\n")})
        self.assertEqual(self.show(fake, {}, split_from="b"), "tmux: no space for new pane")
        self.assertNotIn("set-option", fake.commands())
        fake = Tmux(results={"list-clients": (0, "5 /dev/ttys004 %9 /tmp/#s\n"), "pgrep": (1,)})
        self.assertEqual(self.show(fake, {}, split_from="b"), "tmux would misread '/tmp/#s'" + WATCH)
        self.assertNotIn("split-window", fake.commands())

    def test_split_from_not_shown_anywhere(self):
        for result in ((0, ""), (1, "", "can't find session: =b\n")):
            with self.subTest(result=result):
                fake = Tmux(results={"list-clients": result})
                self.assertTrue(self.show(fake, {}, split_from="b").endswith(WATCH))
                self.assertEqual(fake.commands(), ["list-clients"])

    def test_bad_arguments_raise(self):
        for kw in ({"split_from": "a b"}, {"split_from": "=b"}, {"split": "up"}, {"split": "vertically"}, {"split": ""},
                   {"opener": "a b"}, {"opener": "a:b:c"}):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaises(tui_claude.TuiError):
                    self.show(fake, ITERM, **kw)
                self.assertEqual(fake.calls, [])

    def test_template_ignores_split_split_from_and_opener(self):
        fake = Tmux()
        self.assertIsNone(self.show(fake, INSIDE, template="open {{session}}", split="up", split_from="a b", opener="a b"))
        self.assertEqual(fake.calls, [["/bin/sh", "-c", "open s"]])

    def test_no_anchor_refuses_with_the_attach_command(self):
        for env in ({}, {"ITERM_SESSION_ID": "w0t0p0"}, {"ITERM_SESSION_ID": ""},
                    {"ITERM_SESSION_ID": "w0t0p0:ABC"}, {**ITERM, "TERM_PROGRAM": "Apple_Terminal"}):
            for split in (None, "right"):
                with self.subTest(env=env, split=split):
                    fake = Tmux()
                    why = self.show(fake, env, split=split)
                    self.assertRegex(why, r"^no anchor pane: not in tmux, no iTerm2 pane \(\$ITERM_SESSION_ID\)"
                                     + WATCH + "$")
                    self.assertEqual(fake.calls, [])
                    self.assertEqual(self.stderr.getvalue(), ATTACH + f"tui: show: {why}\n")

    def test_the_watch_command_takes_the_prefix(self):
        for env, prefix in PREFIXES:
            with self.subTest(env=env):
                why = self.show(Tmux(), env)
                self.assertTrue(why.endswith(f"; watch it with {prefix}tmux attach -t '=s'"), why)

    def test_no_tmux(self):
        fake = Tmux()
        self.assertEqual(self.show(fake, ITERM, tmux=None), "tmux not found")
        self.assertEqual(fake.calls, [])

    def test_tmux_path_made_absolute(self):
        here = os.getcwd()
        os.chdir(tempfile.gettempdir())
        self.addCleanup(os.chdir, here)
        fake = Tmux(results={"osascript": (0, "ok NEW\n")})
        self.show(fake, ITERM, tmux="bin/tmux")
        self.assertEqual(fake.calls[2][5], os.path.join(os.getcwd(), "bin/tmux"))

    def test_iterm_session_id_failures_refuse(self):
        missing = FileNotFoundError(2, "No such file or directory", "osascript")
        for results, why in (({"pgrep": (1,)}, "iTerm2 is not running"),
                             ({"osascript": missing}, f"osascript: {missing}"),
                             ({"osascript": subprocess.TimeoutExpired("osascript", 30)},
                              "osascript timed out after 30 s"),
                             ({"osascript": (1, "", "execution error: boom (-1708)\n")},
                              "osascript: execution error: boom (-1708)"),
                             ({"osascript": (0, "no iTerm2 pane shows the anchor\n")},
                              "no iTerm2 pane shows the anchor")):
            with self.subTest(why=why):
                fake = Tmux(results=results)
                self.assertEqual(self.show(fake, ITERM), why + WATCH)
                self.assertEqual(self.stderr.getvalue(), ATTACH + f"tui: show: {why}{WATCH}\n")
                self.assertNotIn("split-window", fake.commands())
                self.assertNotIn("set-option", fake.commands())

    def test_script_is_fixed_and_takes_its_values_as_arguments(self):
        script = tui_claude.APPLESCRIPT
        self.assertNotIn(TMUX, script)
        for part in ("on run argv", 'if application "iTerm2" is running', 'tell application "iTerm2"',
                     "quoted form of tmuxPath", 'quoted form of ("=" & sessionName)',
                     "split vertically with default profile command", "split horizontally with default profile command",
                     "unique id of s", "tty of s", 'return "ok " & (unique id of newSession)'):
            self.assertIn(part, script)

    def test_panes_end_with_their_tmux_session(self):
        self.assertEqual(tui_claude.APPLESCRIPT.count("with default profile command paneCommand"), 2)
        for word in ("write text", "close"):
            self.assertNotIn(word, tui_claude.APPLESCRIPT)


def seq(*results):
    """A Tmux result giving each of results in turn."""
    it = iter(results)
    return lambda argv: next(it)


class Stack(unittest.TestCase):
    """Panes stack by opener. The caller is in tmux session `own`, pane %3, shown by a terminal client (tty
    /dev/ttys004) whose pane is %9; list-sessions prints `rows`, list-panes -a the `live` pane ids (`hosts`, panes()
    rows, for the tty format); pgrep finds no iTerm2 unless told; a tmux split prints %99."""
    NONE = (0, "no iTerm2 pane shows the anchor\n")

    def fake(self, rows=(), live=(), own="cmd", clients=f"5 /dev/ttys004 %9 {SOCK}\n", hosts=(), **results):
        def list_panes(argv):
            return (0, "".join(p + "\n" for p in live)) if argv[-1] == "#{pane_id}" else panes(*hosts)(argv)
        return Tmux(results={"display-message": (0, own + "\n"),
                             "list-sessions": (0, "".join("\t".join(r) + "\n" for r in rows)), "list-panes": list_panes,
                             "list-clients": (0, clients), "pgrep": (1,), "split-window": (0, "%99\n"), **results})

    def show(self, fake, env=INSIDE, **kw):
        self.stderr = io.StringIO()
        with environ(**env), which(), redirect_stderr(self.stderr):
            return tui_claude.show("s", proc=fake, **kw)

    def row(self, number, name, pane, opener="cmd"):
        return f"${number}", name, opener, pane, SOCK

    def test_first_pane_right_of_the_opener(self):
        for results in ({}, {"list-sessions": (1, "", "no server running\n")}):
            with self.subTest(results=results):
                fake = self.fake(**results)
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("cmd"), HOSTS, PGREP,
                                              split_window("-h", "%3"), *record("cmd", "%99")])
                self.assertEqual(self.stderr.getvalue(), ATTACH)
        fake = self.fake(pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.show(fake, ITERM))
        self.assertEqual(fake.calls, [SESSIONS, PGREP, osa("right", "id", "s", "ABC"), *record("w0t0p0:ABC", "NEW")])

    def test_second_below_the_first(self):
        fake = self.fake([self.row(1, "a", "%7")], live=["%3", "%7", "%9"])
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, LIVE, split_window("-v", "%7"), *record("cmd", "%99")])

    def test_newest_closed_goes_below_the_previous_live_one(self):
        rows = [self.row(9, "a", "%9"), self.row(11, "c", "%11"), self.row(10, "b", "%10")]
        for live, pane in ((["%9", "%10", "%11"], "%11"), (["%9", "%10"], "%10"), (["%9"], "%9")):
            with self.subTest(live=live):
                fake = self.fake(rows, live)
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.calls, [OWN, SESSIONS, LIVE, split_window("-v", pane), *record("cmd", "%99")])

    def test_none_live_goes_right(self):
        fake = self.fake([self.row(1, "a", "%7")], live=["%3", "%9"])
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, LIVE, clients_of("cmd"), HOSTS, PGREP, split_window("-h", "%3"),
                                      *record("cmd", "%99")])

    def test_tmux_liveness_is_an_exact_line(self):
        fake = self.fake([self.row(1, "a", "%1")], live=["%10", " %1", "%1 ", "x%1", "%1\r"])
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls[2:], [LIVE, clients_of("cmd"), HOSTS, PGREP, split_window("-h", "%3"),
                                          *record("cmd", "%99")])

    def test_a_worker_gets_its_own_column(self):
        stack = [self.row(1, "a", "%7"), self.row(2, "W", "%8")]   # the commander's (cmd); W's pane is the newest
        live = ["%3", "%7", "%8"]
        fake = self.fake(stack, live, own="W", pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("W"), HOSTS, PGREP,
                                      osa("right", "tty", "s", "/dev/ttys004"), *record("W", "NEW")])
        # W shown by a nested client in the commander's pane %8: that pane splits, never one in W's window
        fake = self.fake(stack, live, own="W", hosts=[("0", "/dev/ttys001", "%7"), ("0", "/dev/ttys004", "%8")])
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("W"), HOSTS, split_window("-h", "%8"),
                                      *record("W", "%99")])
        fake = self.fake([*stack, self.row(3, "w2", "%20", "W")], [*live, "%20"], own="W")
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, LIVE, split_window("-v", "%20"), *record("W", "%99")])

    def test_explicit_split_or_split_from_overrides_the_stack(self):
        for kw, (session, flag, pane) in (({"split": "right"}, ("cmd", "-h", "%3")),
                                          ({"split": "below"}, ("cmd", "-v", "%3")),
                                          ({"split_from": "b"}, ("b", "-h", "%9")),
                                          ({"split_from": "b", "split": "below"}, ("b", "-v", "%9"))):
            with self.subTest(**kw):
                fake = self.fake([self.row(1, "a", "%7")], live=["%3", "%7"])
                self.assertIsNone(self.show(fake, **kw))
                self.assertEqual(fake.calls, [OWN, clients_of(session), HOSTS, PGREP, split_window(flag, pane),
                                              *record("cmd", "%99")])

    def test_iterm2_panes_stack_too(self):
        opener = ITERM["ITERM_SESSION_ID"]
        rows = [self.row(4, "d", "ID4", opener), self.row(3, "c", "%5", opener), self.row(2, "b", "ID2", opener),
                self.row(1, "a", "ID1", opener)]
        tried = [[PGREP, osa("below", "id", "s", "ID4")], [LIVE, PGREP, osa("below", "id", "s", "ID2", "ID1")],
                 [PGREP, osa("right", "id", "s", "ABC")]]
        for n in (1, 2, 3):
            with self.subTest(osascript_calls=n):
                fake = self.fake(rows, pgrep=(0,), osascript=seq(*[self.NONE] * (n - 1), (0, "ok NEW\n")))
                self.assertIsNone(self.show(fake, ITERM))
                self.assertEqual(fake.calls, [SESSIONS, *(c for t in tried[:n] for c in t), *record(opener, "NEW")])
        fake = self.fake(rows, live=["%5"], pgrep=(0,), osascript=self.NONE)
        self.assertIsNone(self.show(fake, ITERM))
        self.assertEqual(fake.calls, [SESSIONS, *tried[0], LIVE, split_window("-v", "%5"), *record(opener, "%99")])
        fake = self.fake(rows)
        self.assertEqual(self.show(fake, ITERM), "iTerm2 is not running" + WATCH)
        self.assertEqual(fake.calls, [SESSIONS, PGREP, LIVE, PGREP, PGREP])

    def test_applescript_output(self):
        for out, pane in (("ok NEW\n", "NEW"), ("ok 6D7E8F90-1A2B\n", "6D7E8F90-1A2B")):
            with self.subTest(out=out):
                fake = self.fake(pgrep=(0,), osascript=(0, out))
                self.assertIsNone(self.show(fake, ITERM))
                self.assertEqual(fake.calls[-2:], record("w0t0p0:ABC", pane))
        for out, why in (("ok\n", "ok"), ("ok \n", "ok"), ("ok a b\n", "ok a b"), ("ok a;b\n", "ok a;b"),
                         ("ok a_b\n", "ok a_b"), ("OK ABC\n", "OK ABC"), ("ok A\nok B\n", "ok A\nok B"),
                         ("", "osascript printed nothing")):
            with self.subTest(out=out):
                fake = self.fake(pgrep=(0,), osascript=(0, out))
                self.assertEqual(self.show(fake, ITERM), why + WATCH)
                self.assertNotIn("set-option", fake.commands())

    def test_options_only_after_a_split(self):
        for template in ("open {{session}}", ""):
            with self.subTest(template=template):
                fake = self.fake()
                self.assertIsNone(self.show(fake, template=template))
                self.assertEqual(fake.calls, [["/bin/sh", "-c", "open s"]] if template else [])
        fake = self.fake(**{"split-window": (1, "", "no space for new pane\n")})
        self.assertEqual(self.show(fake), "tmux: no space for new pane")
        self.assertNotIn("set-option", fake.commands())

    def test_a_failure_to_record_is_printed(self):
        for fail, calls in (("@opener", 1), ("@pane", 2)):
            with self.subTest(fail=fail):
                fake = self.fake(**{"set-option": lambda argv: (1, "", "boom\n") if fail in argv else (0,)})
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.commands().count("set-option"), calls)
                self.assertEqual(self.stderr.getvalue(), ATTACH + f"tui: show: {fail}: tmux: boom\n")
        fake = self.fake(**{"split-window": (0, "\n")})
        self.assertIsNone(self.show(fake))
        self.assertNotIn("set-option", fake.commands())
        self.assertEqual(self.stderr.getvalue(), ATTACH + "tui: show: @pane: not a pane id: ''\n")

    def test_read_back_values_must_fullmatch(self):
        bad = [("$9", "a", "cmd", "%5;", SOCK), ("$8", "b", "cmd", "#{x}", SOCK), ("$7", "c", "cmd", "I D", SOCK),
               ("$7", "d", "cmd", "ID;x", SOCK), ("$6", "e", "cmd ", "%6", SOCK), ("$6", "f", "other", "%6", SOCK),
               ("$6", "g", "c:md:x", "%6", SOCK), ("$5", "s", "cmd", "%5", SOCK), ("5", "h", "cmd", "%5", SOCK),
               ("$5x", "i", "cmd", "%5", SOCK), ("$4", "j", "cmd", "", SOCK), ("$4", "k", "cmd", "%4"),
               ("$4", "l", "cmd", "%4", SOCK, "x")]
        live = ["%2", "%4", "%5", "%6"]
        fake = self.fake(bad, live)
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("cmd"), HOSTS, PGREP, split_window("-h", "%3"),
                                      *record("cmd", "%99")])
        fake = self.fake([*bad, self.row(1, "m", "%2")], live)
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, LIVE, split_window("-v", "%2"), *record("cmd", "%99")])

    def test_a_passed_opener(self):
        fake = self.fake(pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.show(fake, opener="w0t1p0:XYZ"))
        self.assertEqual(fake.calls, [SESSIONS, PGREP, osa("right", "id", "s", "XYZ"), *record("w0t1p0:XYZ", "NEW")])
        fake = self.fake(own="drv")
        self.assertIsNone(self.show(fake, opener="outer"))
        self.assertEqual(fake.calls, [SESSIONS, OWN, clients_of("outer"), HOSTS, PGREP, split_window("-h", "%9"),
                                      *record("outer", "%99")])
        fake = self.fake()
        self.assertIsNone(self.show(fake, ITERM, opener="outer"))
        self.assertEqual(fake.calls, [SESSIONS, clients_of("outer"), HOSTS, PGREP, split_window("-h", "%9"),
                                      *record("outer", "%99")])
        fake = self.fake(own="outer")
        self.assertIsNone(self.show(fake, opener="outer"))
        self.assertEqual(fake.calls[-3:], [split_window("-h", "%3"), *record("outer", "%99")])
        fake = self.fake(own="drv", clients="", pgrep=(0,), osascript=(0, "ok NEW\n"))
        self.assertIsNone(self.show(fake, opener="outer"))
        self.assertEqual(fake.calls, [SESSIONS, OWN, clients_of("outer"), PGREP, osa("right", "id", "s", "ABC"),
                                      *record("outer", "NEW")])
        fake = self.fake([self.row(2, "b", "%8"), self.row(1, "a", "%7", "outer")], ["%7", "%8"], own="drv")
        self.assertIsNone(self.show(fake, opener="outer"))
        self.assertEqual(fake.calls, [SESSIONS, LIVE, split_window("-v", "%7"), *record("outer", "%99")])
        no_iterm = {k: v for k, v in INSIDE.items() if k != "ITERM_SESSION_ID"}
        fake = self.fake(own="drv", clients="")
        self.assertEqual(self.show(fake, no_iterm, opener="outer"), "no anchor pane: no terminal shows tmux session "
                         "outer, no iTerm2 pane ($ITERM_SESSION_ID)" + WATCH)

    def test_a_bad_opener_raises(self):
        for opener in ("", "a b", "w0t0p0:", ":ABC", "a:b:c", "a;b", "w0t0p0:A_B", "=a", "%3", "a\n", "a:b\n"):
            with self.subTest(opener=opener):
                fake = self.fake()
                with self.assertRaisesRegex(tui_claude.TuiError, "opener"):
                    self.show(fake, opener=opener)
                self.assertEqual(fake.calls, [])


class Container(unittest.TestCase):
    """In a Linux container on a Mac: the caller in tmux session `cmd`, pane %0 (tty /dev/pts/0), shown by a client on
    /dev/pts/1 (iTerm2's tmux -CC through docker exec -it); no iTerm2 and no iTerm2 variables. pgrep finds nothing:
    procps, busybox's usage error, or none installed."""
    SOCK = "/tmp/tmux-0/default"
    ENV = {"TMUX": f"{SOCK},42,0", "TMUX_PANE": "%0", "PATH": "/usr/local/bin:/usr/bin:/bin"}
    OWN = ["tmux", "display-message", "-p", "-t", "%0", "#{session_name}"]
    PGREPS = ((1, "", ""), (1, "", "BusyBox v1.36.1 multi-call binary.\n\nUsage: pgrep [-flanovx] PATTERN\n"),
              (2, "", "pgrep: invalid option -- 'a'\n"), FileNotFoundError(2, "No such file or directory", "pgrep"))

    @classmethod
    def fake(cls, pgrep, rows=()):
        def list_panes(argv):
            if argv[-1] == "#{pane_id}":
                return 0, "%0\n%5\n"
            return panes(("0", "/dev/pts/0", "%0"), ("0", "/dev/pts/2", "%5"))(argv)
        return Tmux(results={"display-message": (0, "cmd\n"), "list-panes": list_panes, "pgrep": pgrep,
                             "list-sessions": (0, "".join("\t".join(r) + "\n" for r in rows)),
                             "list-clients": (0, f"100 /dev/pts/1 %0 {cls.SOCK}\n"), "split-window": (0, "%6\n")})

    def show(self, fake, env=ENV):
        self.stderr = io.StringIO()
        with environ(**env), which(), redirect_stderr(self.stderr):
            return tui_claude.show("s", proc=fake)

    def test_first_pane_a_tmux_split_right_of_the_callers(self):
        for pgrep in self.PGREPS:
            with self.subTest(pgrep=pgrep):
                fake = self.fake(pgrep)
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.calls, [self.OWN, SESSIONS, clients_of("cmd"), HOSTS, PGREP,
                                              split_window("-h", "%0", self.SOCK), *record("cmd", "%6")])
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_a_second_pane_stacks_below_the_first(self):
        fake = self.fake(self.PGREPS[0], [("$1", "w1", "cmd", "%5", self.SOCK)])
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [self.OWN, SESSIONS, LIVE, split_window("-v", "%5", self.SOCK),
                                      *record("cmd", "%6")])
        self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_no_client_no_pane_and_the_prefixed_attach_command(self):
        fake = self.fake(self.PGREPS[0])
        fake.results["list-clients"] = (0, "")
        why = self.show(fake, {**self.ENV, "TUI_ATTACH_PREFIX": "docker exec -it box"})
        self.assertTrue(why.startswith("no anchor pane: no terminal shows tmux session cmd"), why)
        self.assertTrue(why.endswith("; watch it with docker exec -it box tmux attach -t '=s'"), why)
        self.assertNotIn("split-window", fake.commands())
        self.assertNotIn("osascript", fake.commands())


class Opener(unittest.TestCase):
    def test_in_tmux_its_session(self):
        fake = Tmux(results={"display-message": (0, "cmd\n")})
        with environ(**INSIDE, TERM_PROGRAM="iTerm.app"):
            self.assertEqual(tui_claude.opener(proc=fake), "cmd")
        self.assertEqual(fake.calls, [OWN])

    def test_in_iterm2_the_full_iterm_session_id(self):
        for value in ("w0t0p0:ABC", "w0t1p0:6D7E8F90-1A2B-4C3D-9E8F-0A1B2C3D4E5F"):
            fake = Tmux()
            with self.subTest(value=value), environ(ITERM_SESSION_ID=value, TERM_PROGRAM="iTerm.app"):
                self.assertEqual(tui_claude.opener(proc=fake), value)
            self.assertEqual(fake.calls, [])

    def test_neither(self):
        for env in ({}, {"ITERM_SESSION_ID": "w0t0p0:ABC"}, {**ITERM, "TERM_PROGRAM": "vscode"},
                    {"TERM_PROGRAM": "iTerm.app"}, {"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": ""}):
            fake = Tmux()
            with self.subTest(env=env), environ(**env), self.assertRaisesRegex(
                    tui_claude.TuiError, r"^no anchor pane: not in tmux, no iTerm2 pane \(\$ITERM_SESSION_ID\)$"):
                tui_claude.opener(proc=fake)
            self.assertEqual(fake.calls, [])

    def test_a_bad_iterm_session_id(self):
        for value in ("w0t0p0", "w0t0p0:", ":ABC", "w0t0p0:a;b", "w0:t0:x", "a b:c", "w0t0p0:#{x}", "w0_t0:x"):
            with self.subTest(value=value), environ(ITERM_SESSION_ID=value, TERM_PROGRAM="iTerm.app"), \
                    self.assertRaisesRegex(tui_claude.TuiError, r"^bad \$ITERM_SESSION_ID"):
                tui_claude.opener(proc=Tmux())

    def test_bad_tmux_pane(self):
        with environ(**{**INSIDE, "TMUX_PANE": "%3;"}), self.assertRaisesRegex(tui_claude.TuiError, "TMUX_PANE"):
            tui_claude.opener(proc=Tmux())

    def test_patterns(self):
        for value in ("cmd", "a_b-1", "w0t0p0:ABC", "w0t1p0:6D7E-8F"):
            self.assertTrue(tui_claude.OPENER.fullmatch(value), value)
        for value in ("", "a b", "a:", ":b", "a:b:c", "a_b:c", "a:b_c", "a;", "%1", "a\n"):
            self.assertFalse(tui_claude.OPENER.fullmatch(value), value)
        self.assertTrue(tui_claude.ITERM_ID.fullmatch("6D7E-8F"))
        for value in ("", "a_b", "a:b", "%1", "a b"):
            self.assertFalse(tui_claude.ITERM_ID.fullmatch(value), value)


class Cli(unittest.TestCase):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = tui_claude.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def patch(self, name, **kw):
        p = unittest.mock.patch.object(tui_claude, name, **kw)
        self.addCleanup(p.stop)
        return p.start()

    def test_start_forms(self):
        start = self.patch("start")
        for argv, session, command, events, template, split, split_from in (
                ("start a -- cmd", "a", ["cmd"], None, None, None, None),
                ("start b --split-from a --split below -- cmd -x", "b", ["cmd", "-x"], None, None, "below", "a"),
                ("start --split below b --show T -- cmd", "b", ["cmd"], None, "T", "below", None),
                ("start a --split right -- cmd -- x", "a", ["cmd", "--", "x"], None, None, "right", None),
                ("start a --events e -- claude", "a", ["claude"], "e", None, None, None),
                ("start b --split-from a -- claude --x -- -p", "b", ["claude", "--x", "--", "-p"], None, None, None,
                 "a")):
            with self.subTest(argv=argv):
                start.reset_mock()
                self.assertEqual(self.main(*argv.split())[0], 0)
                start.assert_called_once_with(session, command, cwd=os.getcwd(), env=dict(os.environ), events=events,
                                              template=template, split=split, split_from=split_from)
        self.main("start", "--show", "", "a", "--", "cmd")
        self.assertEqual(start.call_args.kwargs["template"], "")

    def test_start_usage_errors(self):
        start = self.patch("start")
        for argv in ("start a", "start a --", "start a --split below cmd", "start a --bogus x -- cmd",
                     "start a=b -- cmd", "start --split up a -- cmd", "start b --split-from a=b -- cmd", "start a cmd",
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
        self.assertIn("$TUI_ATTACH_PREFIX", out)
        rc, out, _ = self.main("start", "-h")
        self.assertEqual(rc, 0)
        out = " ".join(out.split())
        self.assertIn("session -- claude", out)
        self.assertIn("must be claude", out)
        for option in ("--show", "--split", "--split-from", "--events FILE"):
            self.assertIn(option, out)

    def test_subcommands(self):
        send, read, show, status = (self.patch(n) for n in ("send", "read", "show", "status"))
        read.return_value, show.return_value, status.return_value = "pane\ntext", None, tui_claude.RUNNING
        self.assertEqual(self.main("send", "s", "hi there"), (0, "", ""))
        send.assert_called_once_with("s", "hi there")
        self.assertEqual(self.main("send", "s", "--", "-x")[0], 0)
        self.assertEqual(send.call_args, unittest.mock.call("s", "-x"))
        self.assertEqual(self.main("read", "s"), (0, "pane\ntext\n", ""))
        self.assertEqual(self.main("read", "s", "--lines", "3")[0], 0)
        self.assertEqual(read.call_args_list, [unittest.mock.call("s", None), unittest.mock.call("s", 3)])
        self.assertEqual(self.main("show", "--show", "x", "--split", "below", "--split-from", "b", "s"), (0, "", ""))
        status.assert_called_once_with("s")
        show.assert_called_once_with("s", "x", split="below", split_from="b")
        self.main("show", "s")
        self.assertEqual(show.call_args, unittest.mock.call("s", None, split=None, split_from=None))

    def test_exit_1(self):
        for name in ("start", "send", "read"):
            self.patch(name, side_effect=tui_claude.TuiError("tmux: boom"))
        for argv in ("start a -- cmd", "send s hi", "read s"):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv.split()), (1, "", "tui: tmux: boom\n"))
        status, show = self.patch("status", return_value=None), self.patch("show", return_value="boom")
        self.assertEqual(self.main("show", "s"), (1, "", "tui: no session s\n"))
        show.assert_not_called()
        status.return_value = tui_claude.RUNNING
        self.assertEqual(self.main("show", "s")[0], 1)

    def test_exit_2(self):
        called = [self.patch(n) for n in ("start", "send", "read", "show", "status")]
        for argv in ((), ("bogus",), ("send", "s"), ("send", "a b", "x"), ("read", "s", "--lines", "0"),
                     ("read", "s", "--lines", "x"), ("read", "s", "--lines", "-1"), ("show", "--split", "up", "s"),
                     ("show", "--split-from", "a b", "s")):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv)[0], 2)
        for mock in called:
            mock.assert_not_called()


class SingleFile(unittest.TestCase):
    def setUp(self):
        with open(tui_claude.__file__) as f:
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
            copy = shutil.copy(tui_claude.__file__, d)
            res = subprocess.run([sys.executable, "-I", copy, "--help"], cwd=d, capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        for word in ("start", "send", "read", "show"):
            self.assertIn(word, res.stdout)


class Exec(unittest.TestCase):
    def test_safe_for_tmux(self):
        self.assertNotIn("#", tui_claude.EXEC)
        self.assertFalse(tui_claude.EXEC.rstrip().endswith(";"))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.path = os.path.join(self.root, "handover.json")

    def run_exec(self, argv, env, cwd, pane, **extra):
        """The wrapper as tmux runs it, given a handover file naming argv."""
        with open(self.path, "w") as f:
            json.dump({"argv": argv, "env": env, "cwd": cwd, **extra}, f)
        return subprocess.run([sys.executable, "-I", "-c", tui_claude.EXEC, self.path], env=pane, capture_output=True,
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

    def test_block_signals_stay_blocked_in_argv(self):
        code = "import json, signal; print(json.dumps(sorted(signal.pthread_sigmask(signal.SIG_BLOCK, []))))"
        for extra, want in (({}, set()), ({"block": [signal.SIGHUP, signal.SIGTERM]}, {signal.SIGHUP, signal.SIGTERM})):
            with self.subTest(**extra):
                res = self.run_exec([sys.executable, "-c", code], {}, self.root, {}, **extra)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(set(json.loads(res.stdout)) & {signal.SIGHUP, signal.SIGTERM}, want)

    def test_default_signal_dispositions(self):
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            with self.subTest(sig=sig.name):
                res = self.run_exec(["/bin/sh", "-c", f"kill -{sig.name[3:]} $$"], {}, self.root, {})
                self.assertEqual(res.returncode, -sig, res.stderr)


if __name__ == "__main__":
    unittest.main()
