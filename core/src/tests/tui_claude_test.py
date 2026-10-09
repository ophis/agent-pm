import ast
import io
import itertools
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
CLIENTS = "#{client_activity} #{client_tty} #{pane_id} #{client_control_mode} #{socket_path}"
PGREP = ["pgrep", "-a", "-x", "iTerm2"]
ITERM = {"ITERM_SESSION_ID": "w0t0p0:ABC", "TERM_PROGRAM": "iTerm.app"}
TMUX_KW = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}
SH_KW = {"stdin": subprocess.DEVNULL, "timeout": 30}
OSA_KW = {**TMUX_KW, "timeout": 30}
TMUX = '/opt/a"b\\c/bin/tmux'   # the script text must never carry it
ATTACH = "tui: session s: tmux attach -t '=s'\n"
WATCH = "; watch it with tmux attach -t '=s'"
MATCHER = "^(?!idle_prompt$)"
# Claude Code's documented notification types, plus one it sends undocumented
NOTIFICATIONS = ("permission_prompt", "idle_prompt", "auth_success", "elicitation_dialog", "elicitation_url_dialog",
                 "elicitation_complete", "elicitation_response", "agent_needs_input", "agent_completed",
                 "quota_auto_resume_fired", "quota_auto_resume_stale", "quota_auto_resume_disabled",
                 "worker_permission_prompt")
STATES = (("Stop", "done"), ("PermissionRequest", "blocked"), ("Notification", "blocked"),
          ("UserPromptSubmit", "working"))
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
# tmux 3.7c #{window_layout}: 3 panes after select-layout main-vertical; 5 tiled, then pane 1 split -h
MAIN_VERTICAL = "7f31,160x48,0,0{80x48,0,0,0,79x48,81,0[79x24,81,0,1,79x23,81,25,2]}"
TILED_SPLIT = ("9a18,160x48,0,0[160x15,0,0{79x15,0,0,0,40x15,80,0,1,39x15,121,0,5},"
               "160x15,0,16{79x15,0,16,2,80x15,80,16,3},160x16,0,32,4]")
GRID_PANES = "#{window_id}\t#{@grid-manager}\t#{pane_id}\t#{pane_width}\t#{pane_height}\t#{socket_path}"
PY, SCRIPT = "/usr/bin/python3", "/opt/a_b.c@1+2-3/tui_claude.py"   # sys.executable and the module's path, patched


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


def grid_read(pane):
    return ["tmux", "list-panes", "-t", pane, "-F", GRID_PANES]


def grid_set(window, manager, n=3, hooks=True):
    """The one tmux call setting a grid window's options and, with hooks, its three hooks."""
    hook = f"run-shell -b '{PY} -I {SCRIPT} tile {window} >/dev/null 2>&1 || true'"
    return ["tmux", "set-option", "-w", "-t", window, "@grid-manager", manager, ";",
            "set-option", "-w", "-t", window, "@grid-per-column", str(n),
            *(a for h in ("pane-exited", "window-resized", "window-layout-changed") if hooks
              for a in (";", "set-hook", "-w", "-t", window, h, hook))]


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
    prints (None: no server). On new-session or respawn-pane it reads the handover file named in its argv, then unlinks
    it as the wrapper would, unless `hand_over` is False."""

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
        if argv[1] == "new-session" or "respawn-pane" in argv:
            self.path = argv[argv.index(";") - 1] if argv[1] == "new-session" else argv[-1]
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
        env = {"PATH": self.bin, "HOME": "/h", "CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_JOB_DIR": "/j",
               **{k: "x" for k in tui_claude.TERMINAL_KEYS}}
        fake = Tmux()
        self.start(fake, env=env)
        self.assertEqual(fake.handover, {"argv": [self.tool, "--settings", tui_claude.hooks(), "-x", "a b"],
                                         "env": {"PATH": self.bin, "HOME": "/h", "CLAUDECODE": "1", "PWD": self.cwd},
                                         "cwd": self.cwd})
        self.assertEqual(fake.mode, 0o600)
        self.assertEqual(fake.dir_mode, 0o700)
        self.assertEqual(os.path.dirname(os.path.dirname(fake.path)), self.temp)
        self.assertEqual(os.listdir(self.temp), [])

    def test_pwd_is_the_cwd(self):
        for given in ({"PATH": self.bin, "PWD": "/elsewhere"}, {"PATH": self.bin}):
            with self.subTest(given=given):
                fake = Tmux()
                self.start(fake, env=given)
                self.assertEqual(fake.handover["env"]["PWD"], self.cwd)

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
            self.start(fake, template="tmpl", split="below", split_from="b", opener="o", per_column=2)
        self.assertEqual(seen, [(("s", "tmpl"), {"split": "below", "split_from": "b", "opener": "o", "per_column": 2,
                                                 "proc": fake}, False, ["display-message", "new-session", *DECORATE])])

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
                                           "list-sessions", "list-clients", "split-window", "set-option", "set-option"])
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

    def test_bad_per_column_refused_before_tmux(self):
        for bad in (0, 10000, True, "3"):
            with self.subTest(per_column=bad):
                fake = Tmux()
                with self.assertRaisesRegex(tui_claude.TuiError, "^per_column must be an int from 1 to 9999"):
                    self.start(fake, template=None, per_column=bad)
                self.assertEqual(fake.calls, [])
        fake = Tmux()
        self.start(fake, template="", per_column=0)
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


class Respawn(unittest.TestCase):
    setUp = Start.setUp

    def respawn(self, fake, session="s", argv=("tool", "-x", "a b"), env=None):
        env = {"PATH": self.bin, "HOME": "/h"} if env is None else env
        tui_claude.respawn(session, list(argv), cwd=self.cwd, env=env, proc=fake, sleep=self.sleep)

    def test_clears_the_history_and_respawns_the_pane_running_the_wrapper_in_one_tmux_command(self):
        fake = Tmux()
        self.respawn(fake)
        self.assertEqual(fake.calls, [["tmux", "clear-history", "-t", "=s:", ";", "respawn-pane", "-k", "-t", "=s:",
                                       sys.executable, "-I", "-c", tui_claude.EXEC, fake.path]])
        self.assertEqual(fake.kwargs, [TMUX_KW])
        self.sleep.assert_not_called()

    def test_handover_file_as_start_writes_it(self):
        env = {"PATH": self.bin, "HOME": "/h", "PWD": "/stale",
               **{k: "x" for k in (*tui_claude.PARENT_KEYS, *tui_claude.TERMINAL_KEYS)}}
        fake = Tmux()
        self.respawn(fake, env=env)
        self.assertEqual(fake.handover, {"argv": [self.tool, "-x", "a b"], "cwd": self.cwd,
                                         "env": {"PATH": self.bin, "HOME": "/h", "PWD": self.cwd}})
        self.assertEqual(fake.mode, 0o600)
        self.assertEqual(os.listdir(self.temp), [])

    def test_cwd_never_reaches_tmux(self):
        self.cwd = os.path.join(self.root, "#(x)#{y}")
        os.makedirs(self.cwd)
        fake = Tmux()
        self.respawn(fake)
        self.assertEqual(fake.handover["cwd"], self.cwd)
        self.assertEqual([a for c in fake.calls for a in c if "#(x)" in a or "#{y}" in a], [])

    def test_refused_before_tmux(self):
        for kw in ({"session": "a b"}, {"argv": ()}, {"argv": ("no-such-tool",)}):
            with self.subTest(**kw):
                fake = Tmux()
                with self.assertRaises(tui_claude.TuiError):
                    self.respawn(fake, **kw)
                self.assertEqual(fake.calls, [])
        with unittest.mock.patch.object(sys, "executable", "/opt/py#3/bin/python3"), \
                self.assertRaisesRegex(tui_claude.TuiError, "tmux would misread"):
            self.respawn(fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(os.listdir(self.temp), [])

    def test_a_failed_respawn(self):
        fake = Tmux(fail={"clear-history"})
        with self.assertRaisesRegex(tui_claude.TuiError, r"^tmux: boom$"):
            self.respawn(fake)
        self.assertEqual(fake.commands(), ["clear-history"])
        self.assertEqual(os.listdir(self.temp), [])

    def test_wrapper_never_reads(self):
        fake = Tmux(hand_over=False)
        with self.assertRaisesRegex(tui_claude.TuiError, r"^the pane did not respawn$"):
            self.respawn(fake)
        self.assertAlmostEqual(sum(c.args[0] for c in self.sleep.call_args_list), tui_claude.HANDOVER_TIMEOUT)
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
                                 {"Stop": [{"hooks"}], "PermissionRequest": [{"hooks"}],
                                  "Notification": [{"matcher", "hooks"}], "UserPromptSubmit": [{"hooks"}]})
                self.assertEqual(hooks["hooks"]["Notification"][0]["matcher"], MATCHER)
                for entries in hooks["hooks"].values():
                    self.assertEqual([h["type"] for h in entries[0]["hooks"]], ["command"])

    def test_notification_matcher_is_every_type_but_idle_prompt(self):
        """MATCHER is on Claude Code's regex path, where re.search agrees with JavaScript's RegExp.prototype.test."""
        self.assertIsNone(re.fullmatch(r"[A-Za-z0-9_|, -]*", tui_claude.MATCHER))
        self.assertEqual([t for t in (*NOTIFICATIONS, "idle_prompt_x", "") if not re.search(tui_claude.MATCHER, t)],
                         ["idle_prompt"])

    def test_state_without_events(self):
        for event, word in STATES:
            with self.subTest(event=event):
                res, calls = self.run_hook(None, event)
                self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
                self.assertEqual(calls, [f"set-option -t %1 @state {word}"])
        self.assertEqual(os.listdir(self.root), ["bin"])

    def test_state_and_event_lines_with_events(self):
        for sub in ("a b", "it's", "$(touch x)"):
            events = os.path.join(self.root, sub, "e")
            os.makedirs(os.path.dirname(events))
            for event, word in STATES:
                with self.subTest(sub=sub, event=event):
                    res, calls = self.run_hook(events, event)
                    self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
                    self.assertEqual(calls[0], f"set-option -t %1 @state {word}")
            with open(events) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 3)
            for line, word in zip(lines, ("done", "blocked", "blocked")):
                self.assertRegex(line, rf"^\d\d:\d\d:\d\d w1 {word}\n$")
        self.assertFalse(os.path.exists(os.path.join(self.root, "x")))

    def test_outside_tmux_a_no_op(self):
        events = os.path.join(self.root, "e")
        for event, _ in STATES:
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
                                                         "PermissionRequest": ours["PermissionRequest"],
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
        """A list-clients result: rows of (activity, tty, pane[, control mode, default "0"])."""
        rows = [(*r, "0") if len(r) == 3 else r for r in rows]
        return 0, "".join(f"{at} {tty} {pane} {control} {SOCK}\n" for at, tty, pane, control in rows)

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
        control = self.clients((5, "/dev/ttys004", "%9", "1"))
        mixed = self.clients((5, "/dev/ttys004", "%9"), (3, "/dev/ttys006", "%8", "1"))
        nested = panes(("0", "/dev/ttys001", "%1"), ("0", "/dev/ttys004", "%5"))
        cases = [(ITERM, {}, None, tui_claude.Anchor(("id", ["ABC"]))),
                 (INSIDE, {**own, "list-clients": shown}, None,
                  tui_claude.Anchor(("tty", ["/dev/ttys004"]), "%3", SOCK, "own")),
                 (INSIDE, {**own, "list-clients": shown, "list-panes": nested}, None,
                  tui_claude.Anchor(None, "%5", SOCK, "own")),
                 (INSIDE, own, None, tui_claude.Anchor(("id", ["ABC"]))),
                 ({}, {"list-clients": shown}, "b", tui_claude.Anchor(("tty", ["/dev/ttys004"]), "%9", SOCK, "b")),
                 ({}, {"list-clients": shown, "list-panes": nested}, "b", tui_claude.Anchor(None, "%5", SOCK, "b")),
                 ({}, {"list-clients": (0, "5 /dev/ttys004\n7 /dev/ttys005 junk 0 /s\n7 /dev/ttys005 %7 /s\n"
                                           "7 /dev/ttys005 %7 2 /s\n" + shown[1])}, "b",
                  tui_claude.Anchor(("tty", ["/dev/ttys004"]), "%9", SOCK, "b")),
                 (INSIDE, {**own, "list-clients": control, "list-panes": nested}, None,
                  tui_claude.Anchor(None, "%3", SOCK, "own")),
                 ({}, {"list-clients": control, "list-panes": nested}, "b", tui_claude.Anchor(None, "%9", SOCK, "b")),
                 ({}, {"list-clients": mixed}, "b", tui_claude.Anchor(("tty", ["/dev/ttys004"]), "%9", SOCK, "b"))]
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
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("b"), HOSTS, PGREP,
                                      osa("below", "tty", "s", "/dev/ttys004"), *record("own", "NEW")])

    def test_tmux_split_split_from(self):
        clients = self.clients((5, "/dev/ttys004", "%9"), (9, "/dev/ttys005", "%7"))
        fake = Tmux(results={"list-clients": clients, "pgrep": (1,), "split-window": (0, "%10\n")})
        self.assertIsNone(self.show(fake, {}, split_from="b", split="below"))
        self.assertEqual(fake.calls[-2:], [split_window("-v", "%7"), *record(None, "%10")])
        nested = Tmux(results={"list-clients": clients, "list-panes": panes(("0", "/dev/ttys005", "%5")),
                               "split-window": (0, "%10\n")})
        self.assertIsNone(self.show(nested, {}, split_from="b"))
        self.assertEqual(nested.calls, [clients_of("b"), HOSTS, split_window("-h", "%5"), *record(None, "%10")])

    def test_control_mode_client_gets_a_tmux_split_of_own_pane(self):
        # iTerm2's tmux -CC: the control-mode client's tty is the hidden gateway tab's; iTerm2 draws the tmux split
        clients = self.clients((300, "/dev/ttys009", "%1", "1"), (100, "/dev/ttys001", "%2"))
        for split, flag, head in ((None, "-h", [OWN, SESSIONS]), ("right", "-h", [OWN, SESSIONS]),
                                  ("below", "-v", [OWN, SESSIONS])):
            with self.subTest(split=split):
                fake = Tmux(results={"display-message": (0, "own\n"), "list-clients": clients,
                                     "split-window": (0, "%10\n")})
                self.assertIsNone(self.show(fake, INSIDE, split=split))
                self.assertEqual(fake.calls, [*head, clients_of("own"), split_window(flag, "%3"),
                                              *record("own", "%10")])
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_split_from_a_session_only_a_control_mode_client_shows(self):
        clients = self.clients((5, "/dev/ttys009", "%9", "1"))
        for env, head, opener in (({}, [], None), (INSIDE, [OWN, SESSIONS], "own")):
            with self.subTest(env=env):
                fake = Tmux(results={"display-message": (0, "own\n"), "list-clients": clients,
                                     "split-window": (0, "%10\n")})
                self.assertIsNone(self.show(fake, env, split_from="b", split="below"))
                self.assertEqual(fake.calls, [*head, clients_of("b"), split_window("-v", "%9"), *record(opener, "%10")])

    def test_control_mode_ttys_are_never_iterm2_anchors(self):
        fake = Tmux(results={"display-message": (0, "own\n"), "osascript": (0, "ok NEW\n"),
                             "list-clients": self.clients((300, "/dev/ttys003", "%2"), (200, "/dev/ttys009", "%1", "1"),
                                                          (100, "/dev/ttys001", "%1"))})
        self.assertIsNone(self.show(fake, INSIDE))
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("own"), HOSTS, PGREP,
                                      osa("right", "tty", "s", "/dev/ttys003", "/dev/ttys001"), *record("own", "NEW")])

    def test_tmux_split_failures(self):
        clients = self.clients((5, "/dev/ttys004", "%9"))
        fake = Tmux(results={"list-clients": clients, "pgrep": (1,), "split-window": (1, "", "no space for new pane\n")})
        self.assertEqual(self.show(fake, {}, split_from="b"), "tmux: no space for new pane")
        self.assertNotIn("set-option", fake.commands())
        fake = Tmux(results={"list-clients": (0, "5 /dev/ttys004 %9 0 /tmp/#s\n"), "pgrep": (1,)})
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

    def fake(self, rows=(), live=(), own="cmd", clients=f"5 /dev/ttys004 %9 0 {SOCK}\n", hosts=(), **results):
        def list_panes(argv):
            if "-a" not in argv:   # a grid read: a window with no grid
                return 0, f"@1\t\t{argv[3]}\t80\t24\t{SOCK}\n"
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
        self.assertEqual(fake.calls, [OWN, SESSIONS, grid_read("%8"), clients_of("W"), HOSTS, PGREP,
                                      osa("right", "tty", "s", "/dev/ttys004"), *record("W", "NEW")])
        # W shown by a nested client in the commander's pane %8: that pane splits, never one in W's window
        fake = self.fake(stack, live, own="W", hosts=[("0", "/dev/ttys001", "%7"), ("0", "/dev/ttys004", "%8")])
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, grid_read("%8"), clients_of("W"), HOSTS, split_window("-h", "%8"),
                                      *record("W", "%99")])
        fake = self.fake([*stack, self.row(3, "w2", "%20", "W")], [*live, "%20"], own="W")
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, grid_read("%8"), LIVE, split_window("-v", "%20"),
                                      *record("W", "%99")])

    def test_explicit_split_or_split_from_overrides_the_stack(self):
        for kw, (session, flag, pane) in (({"split": "right"}, ("cmd", "-h", "%3")),
                                          ({"split": "below"}, ("cmd", "-v", "%3")),
                                          ({"split_from": "b"}, ("b", "-h", "%9")),
                                          ({"split_from": "b", "split": "below"}, ("b", "-v", "%9"))):
            with self.subTest(**kw):
                fake = self.fake([self.row(1, "a", "%7")], live=["%3", "%7"])
                self.assertIsNone(self.show(fake, **kw))
                self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of(session), HOSTS, PGREP,
                                              split_window(flag, pane), *record("cmd", "%99")])

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
    /dev/pts/1 (iTerm2's tmux -CC through docker exec -it, control mode; else a plain docker exec -it tmux attach); no
    iTerm2 and no iTerm2 variables. pgrep finds nothing: procps, busybox's usage error, or none installed."""
    SOCK = "/tmp/tmux-0/default"
    ENV = {"TMUX": f"{SOCK},42,0", "TMUX_PANE": "%0", "PATH": "/usr/local/bin:/usr/bin:/bin"}
    OWN = ["tmux", "display-message", "-p", "-t", "%0", "#{session_name}"]
    PGREPS = ((1, "", ""), (1, "", "BusyBox v1.36.1 multi-call binary.\n\nUsage: pgrep [-flanovx] PATTERN\n"),
              (2, "", "pgrep: invalid option -- 'a'\n"), FileNotFoundError(2, "No such file or directory", "pgrep"))

    @classmethod
    def fake(cls, pgrep, rows=(), control="1"):
        def list_panes(argv):
            if argv[-1] == "#{pane_id}":
                return 0, "%0\n%5\n"
            return panes(("0", "/dev/pts/0", "%0"), ("0", "/dev/pts/2", "%5"))(argv)
        return Tmux(results={"display-message": (0, "cmd\n"), "list-panes": list_panes, "pgrep": pgrep,
                             "list-sessions": (0, "".join("\t".join(r) + "\n" for r in rows)),
                             "list-clients": (0, f"100 /dev/pts/1 %0 {control} {cls.SOCK}\n"),
                             "split-window": (0, "%6\n")})

    def show(self, fake, env=ENV):
        self.stderr = io.StringIO()
        with environ(**env), which(), redirect_stderr(self.stderr):
            return tui_claude.show("s", proc=fake)

    def test_first_pane_a_tmux_split_right_of_the_callers(self):
        fake = self.fake(self.PGREPS[0])
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [self.OWN, SESSIONS, clients_of("cmd"), split_window("-h", "%0", self.SOCK),
                                      *record("cmd", "%6")])
        self.assertEqual(self.stderr.getvalue(), ATTACH)
        for pgrep in self.PGREPS:
            with self.subTest(pgrep=pgrep):
                fake = self.fake(pgrep, control="0")
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


class GridOpen(unittest.TestCase):
    """Grid mode. The caller is in root session mgr (no @opener), pane %3, shown by a terminal client (tty
    /dev/ttys004, its pane %9); `windows` maps a pane to its window's list-panes rows (window, @grid-manager, pane,
    width, height), a pane not in it is gone; list-panes -a prints nothing; iTerm2 runs; a split prints %20; tile
    records its call in the fake's calls."""
    MGR = ("$1", "mgr", "", "")
    NEW = [("@1", "", "%3", "160", "48")]   # mgr's window, no grid yet
    NOTICE = "tui: show: grid: --split/--split-from ignored\n"

    def setUp(self):
        self.tile = unittest.mock.Mock(side_effect=lambda window, *, per_column, proc:
                                       proc.calls.append(["tile", window, per_column]))
        for p in (unittest.mock.patch.object(tui_claude, "tile", self.tile),
                  unittest.mock.patch.object(sys, "executable", PY),
                  unittest.mock.patch.object(tui_claude, "__file__", SCRIPT)):
            p.start()
            self.addCleanup(p.stop)

    def fake(self, windows, rows=(MGR,), own="mgr", clients=f"5 /dev/ttys004 %9 0 {SOCK}\n", **results):
        def list_panes(argv):
            if "-a" in argv:
                return 0, ""
            if argv[3] not in windows:
                return 1, "", f"can't find pane: {argv[3]}\n"
            return 0, "".join("\t".join((*r, SOCK)) + "\n" for r in windows[argv[3]])
        return Tmux(results={"display-message": (0, own + "\n"),
                             "list-sessions": (0, "".join("\t".join((*r, SOCK)) + "\n" for r in rows)),
                             "list-clients": (0, clients), "list-panes": list_panes, "split-window": (0, "%20\n"),
                             "pgrep": (0,), "osascript": (0, "ok NEW\n"), **results})

    def show(self, fake, env=INSIDE, **kw):
        self.stderr = io.StringIO()
        with environ(**env), which(), redirect_stderr(self.stderr):
            return tui_claude.show("s", proc=fake, **kw)

    def opened(self, window="@1", manager="%3", opener="mgr", n=3):
        """What follows a grid split: the records, the grid's options and hooks, then tile."""
        return [*record(opener, "%20"), grid_set(window, manager, n), ["tile", window, n]]

    def test_own_root_starts_a_grid_right_of_its_pane(self):
        # plain tmux in iTerm2 ($ITERM_SESSION_ID set, iTerm2 running): a tmux split all the same
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("mgr"), grid_read("%3"), split_window("-h", "%3"),
                                      *self.opened()])
        self.assertEqual(self.stderr.getvalue(), ATTACH)
        # its @pane gone: still a root
        fake = self.fake({"%3": self.NEW}, [("$1", "mgr", "", "%77")])
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, grid_read("%77"), clients_of("mgr"), grid_read("%3"),
                                      split_window("-h", "%3"), *self.opened()])

    def test_the_largest_pane_but_the_managers_splits_below(self):
        for workers, target in (([("%10", "80", "24"), ("%11", "80", "23")], "%10"),
                                ([("%12", "40", "48"), ("%8", "80", "24"), ("%4", "20", "10")], "%12"),
                                ([("%8", "80", "24"), ("%12", "40", "48")], "%12")):
            with self.subTest(target=target, workers=workers):
                fake = self.fake({"%3": [("@1", "%3", "%3", "79", "48"), *(("@1", "%3", *w) for w in workers)]})
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.calls[3:], [grid_read("%3"), split_window("-v", target), *self.opened()])

    def test_a_stored_manager_not_a_pane_of_the_window_starts_a_new_grid_at_the_anchor(self):
        for manager in ("%99", "x", "%3;", " %3"):
            with self.subTest(manager=manager):
                fake = self.fake({"%3": [("@1", manager, "%3", "79", "48"), ("@1", manager, "%5", "80", "48")]})
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.calls[3:], [grid_read("%3"), split_window("-v", "%5"), *self.opened()])

    def test_a_root_not_the_callers_anchors_at_its_clients_pane(self):
        # the most recently active client's pane; a control-mode client (iTerm2's tmux -CC) too
        clients = f"5 /dev/ttys004 %8 0 {SOCK}\n9 /dev/ttys009 %9 1 {SOCK}\n"
        for env, head in ((INSIDE, [SESSIONS, clients_of("mgr"), OWN]), ({}, [SESSIONS, clients_of("mgr")])):
            with self.subTest(env=env):
                fake = self.fake({"%9": [("@2", "", "%9", "160", "48")]}, own="drv", clients=clients)
                self.assertIsNone(self.show(fake, env, opener="mgr"))
                self.assertEqual(fake.calls, [*head, grid_read("%9"), split_window("-h", "%9"),
                                              *self.opened("@2", "%9")])
        # the client's pane in a grid window: that grid
        fake = self.fake({"%9": [("@1", "%3", "%3", "79", "48"), ("@1", "%3", "%9", "80", "48")]}, own="drv")
        self.assertIsNone(self.show(fake, opener="mgr"))
        self.assertEqual(fake.calls[3:], [grid_read("%9"), split_window("-v", "%9"), *self.opened()])

    def test_a_given_opener_that_is_the_callers_session_anchors_at_its_pane(self):
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.show(fake, opener="mgr"))
        self.assertEqual(fake.calls, [SESSIONS, clients_of("mgr"), OWN, grid_read("%3"), split_window("-h", "%3"),
                                      *self.opened()])

    def test_a_member_opens_a_sub_worker_in_its_window(self):
        rows = (self.MGR, ("$2", "w1", "mgr", "%10"))
        window = [("@1", "%3", "%3", "79", "48"), ("@1", "%3", "%10", "80", "48")]
        for kw, head in (({}, [OWN, SESSIONS]), ({"opener": "w1"}, [SESSIONS])):
            with self.subTest(**kw):
                fake = self.fake({"%10": window}, rows, own="w1")
                self.assertIsNone(self.show(fake, **kw))
                self.assertEqual(fake.calls, [*head, grid_read("%10"), split_window("-v", "%10"),
                                              *self.opened(opener="w1")])

    def test_todays_path_for_a_detached_root_or_a_member_outside_a_grid(self):
        fake = self.fake({"%3": self.NEW}, clients="")
        self.assertIsNone(self.show(fake))
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("mgr"), clients_of("mgr"), PGREP,
                                      osa("right", "id", "s", "ABC"), *record("mgr", "NEW")])
        rows = (self.MGR, ("$2", "w1", "x", "%10"))
        for windows in ({"%10": [("@1", "", "%10", "160", "48")]}, {"%10": [("@1", "%9", "%10", "160", "48")]}, {}):
            with self.subTest(windows=windows):
                fake = self.fake(windows, rows, own="w1", pgrep=(1,))
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.calls, [OWN, SESSIONS, grid_read("%10"), clients_of("w1"), HOSTS, PGREP,
                                              split_window("-h", "%3"), *record("w1", "%20")])
                self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_a_failed_or_unreadable_grid_read_falls_back_to_todays_path(self):
        for windows in ({}, *({"%3": [row]} for row in (
                ("@1;", "", "%3", "160", "48"), ("1", "", "%3", "160", "48"), ("@1", "", "%3;", "160", "48"),
                ("@1", "", "%3", "x", "48"), ("@1", "", "%3", "160", "²"), ("@1", "", "%3", "160")))):
            with self.subTest(windows=windows):
                fake = self.fake(windows, pgrep=(1,))
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("mgr"), grid_read("%3"), clients_of("mgr"),
                                              HOSTS, PGREP, split_window("-h", "%3"), *record("mgr", "%20")])
        fake = self.fake({"%3": self.NEW}, **{"list-clients": (1, "", "boom\n")})
        self.assertEqual(self.show(fake), "tmux: boom" + WATCH)
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("mgr"), clients_of("mgr")])

    def test_split_and_split_from_ignored_in_the_grid(self):
        for kw in ({"split": "below"}, {"split_from": "b"}, {"split": "right", "split_from": "b"}):
            with self.subTest(**kw):
                fake = self.fake({"%3": self.NEW})
                self.assertIsNone(self.show(fake, **kw))
                self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("mgr"), grid_read("%3"),
                                              split_window("-h", "%3"), *self.opened()])
                self.assertEqual(self.stderr.getvalue(), ATTACH + self.NOTICE)

    def test_split_honored_outside_the_grid(self):
        fake = self.fake({"%3": self.NEW}, clients="")
        self.assertIsNone(self.show(fake, split="below"))
        self.assertEqual(fake.calls, [OWN, SESSIONS, clients_of("mgr"), clients_of("mgr"), PGREP,
                                      osa("below", "id", "s", "ABC"), *record("mgr", "NEW")])
        self.assertEqual(self.stderr.getvalue(), ATTACH)

    def test_a_failed_split_changes_nothing_else(self):
        fake = self.fake({"%3": self.NEW}, **{"split-window": (1, "", "no space for new pane\n")})
        self.assertEqual(self.show(fake), "tmux: no space for new pane")
        self.assertEqual(fake.calls[-1], split_window("-h", "%3"))
        self.assertEqual(self.stderr.getvalue(), ATTACH + "tui: show: tmux: no space for new pane\n")

    def test_hooks_need_paths_tmux_takes_verbatim(self):
        for target, name, bad in ((sys, "executable", "/a b/python3"), (sys, "executable", "/a#b/python3"),
                                  (sys, "executable", "/a/python3;"), (sys, "executable", "/a/pythön"),
                                  (sys, "executable", ""), (sys, "executable", None),
                                  (tui_claude, "__file__", "/a'b/tui_claude.py"),
                                  (tui_claude, "__file__", "/a$(x)/tui_claude.py")):
            with self.subTest(name=name, bad=bad), unittest.mock.patch.object(target, name, bad):
                fake = self.fake({"%3": self.NEW})
                self.assertIsNone(self.show(fake))
                self.assertEqual(fake.calls[-4:], [*record("mgr", "%20"), grid_set("@1", "%3", hooks=False),
                                                   ["tile", "@1", 3]])
                self.assertEqual(self.stderr.getvalue(),
                                 ATTACH + f"tui: show: grid: hooks: {bad or ''}: tmux would misread it\n")

    def test_a_failure_after_the_split_is_the_shows_and_leaves_the_pane(self):
        fake = self.fake({"%3": self.NEW}, **{"set-option": lambda argv: (1, "", "boom\n") if "-w" in argv else (0,)})
        self.assertEqual(self.show(fake), "tmux: boom")
        self.assertEqual(fake.calls[-3:], [*record("mgr", "%20"), grid_set("@1", "%3")])
        self.tile.side_effect = tui_claude.TuiError("tmux: boom")
        fake = self.fake({"%3": self.NEW})
        self.assertEqual(self.show(fake), "tmux: boom")
        self.assertEqual(fake.calls[-3:], [*record("mgr", "%20"), grid_set("@1", "%3")])
        self.assertNotIn("kill-session", fake.commands())
        self.assertEqual(self.stderr.getvalue(), ATTACH + "tui: show: tmux: boom\n")

    def test_per_column(self):
        fake = self.fake({"%3": self.NEW})
        self.assertIsNone(self.show(fake, per_column=2))
        self.assertEqual(fake.calls[-2:], [grid_set("@1", "%3", 2), ["tile", "@1", 2]])
        for bad in (0, 10000, -1, True, "3", 2.0):
            with self.subTest(per_column=bad):
                fake = self.fake({"%3": self.NEW})
                for call in (lambda: self.show(fake, per_column=bad),
                             lambda: tui_claude.open_pane("s", per_column=bad, proc=fake)):
                    with self.assertRaisesRegex(tui_claude.TuiError, "^per_column must be an int from 1 to 9999"):
                        call()
                self.assertEqual(fake.calls, [])


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


def slot(pane, *children):
    return tui_claude.Slot(pane, children)


def body(cell):
    return tui_claude._render_layout(cell).split(",", 1)[1]


def dump(text):
    """A layout body as #{window_layout} prints it, with its checksum."""
    return f"{tui_claude._checksum(text)},{text}"


def grid(cells, per_column=3, width=160, height=48, manager_width=79):
    """The grid of manager %0 in a width x height window; cells: main cells, or n for %1..%n."""
    if isinstance(cells, int):
        cells = [slot(f"%{i}") for i in range(1, cells + 1)]
    return tui_claude._grid_layout(cells, width=width, height=height, manager="%0", manager_width=manager_width,
                                   per_column=per_column)


class GridLayout(unittest.TestCase):
    """Window 160x48, manager %0 79 wide."""

    def tiles(self, cell):
        """Each container's children tile it exactly, one-cell borders between them."""
        if cell.kind == tui_claude.LEAF:
            return
        self.assertGreaterEqual(len(cell.children), 2)
        across = cell.kind == tui_claude.LEFT_RIGHT
        at = cell.x if across else cell.y
        for child in cell.children:
            if across:
                self.assertEqual((child.x, child.y, child.h), (at, cell.y, cell.h))
            else:
                self.assertEqual((child.y, child.x, child.w), (at, cell.x, cell.w))
            at += (child.w if across else child.h) + 1
            self.tiles(child)
        self.assertEqual(at - 1, cell.x + cell.w if across else cell.y + cell.h)

    def check(self, root, n, per_column, width=160, height=48):
        """FR-3: main cell i (pane %i+1) in column i // N, row i % N; equal widths and heights, each +-1."""
        self.tiles(root)
        self.assertEqual((root.kind, root.w, root.h, root.x, root.y), (tui_claude.LEFT_RIGHT, width, height, 0, 0))
        manager, area = root.children
        self.assertEqual((manager.pane, manager.h), ("%0", height))
        c = -(-n // per_column)
        if c > 1:
            self.assertEqual(area.kind, tui_claude.LEFT_RIGHT)
        columns = list(area.children) if c > 1 else [area]
        self.assertEqual(len(columns), c)
        self.assertLessEqual(max(col.w for col in columns) - min(col.w for col in columns), 1)
        for j, column in enumerate(columns):
            cells = list(column.children) if column.kind == tui_claude.TOP_BOTTOM else [column]
            self.assertEqual([cell.pane for cell in cells],
                             [f"%{i + 1}" for i in range(j * per_column, min(n, (j + 1) * per_column))])
            self.assertLessEqual(max(cell.h for cell in cells) - min(cell.h for cell in cells), 1)

    def test_fr3_per_column_3(self):
        for n, want in ((1, "160x48,0,0{79x48,0,0,0,80x48,80,0,1}"),
                        (2, "160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0,1,80x23,80,25,2]}"),
                        (4, "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0[40x16,80,0,1,40x15,80,17,2,40x15,80,33,3],"
                            "39x48,121,0,4}}"),
                        (7, "160x48,0,0{79x48,0,0,0,80x48,80,0{26x48,80,0[26x16,80,0,1,26x15,80,17,2,26x15,80,33,3],"
                            "26x48,107,0[26x16,107,0,4,26x15,107,17,5,26x15,107,33,6],26x48,134,0,7}}")):
            with self.subTest(n=n):
                root = grid(n)
                self.assertEqual(body(root), want)
                self.check(root, n, 3)

    def test_fr3_any_count(self):
        for per_column in (1, 2, 3, 4):
            for n in range(1, 13):
                with self.subTest(per_column=per_column, n=n):
                    self.check(grid(n, per_column), n, per_column)

    def test_per_column_2_three_cells(self):
        self.assertEqual(body(grid(3, 2)),
                         "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0[40x24,80,0,1,40x23,80,25,2],39x48,121,0,3}}")

    def test_manager_width_kept(self):
        for width in (1, 30, 100):
            with self.subTest(width=width):
                root = grid(4, manager_width=width)
                self.assertEqual(root.children[0], tui_claude.Cell(width, 48, 0, 0, "%0"))
                self.check(root, 4, 3)

    def test_compress_when_too_wide(self):
        root = grid(7, manager_width=158)
        self.assertEqual(root.children[0].w, 154)
        self.assertEqual([col.w for col in root.children[1].children], [1, 1, 1])
        self.check(root, 7, 3)
        self.assertEqual(body(grid([slot("%1", slot("%2"))], manager_width=200)),
                         "160x48,0,0{156x48,0,0,0,3x48,157,0{1x48,157,0,1,1x48,159,0,2}}")
        self.assertEqual(grid([slot("%1", slot("%2", slot("%3")))], manager_width=200).children[0].w, 152)

    def test_infeasible(self):
        self.assertEqual(grid(7, width=7).children[0].w, 1)
        self.assertIsNone(grid(7, width=6))
        self.assertIsNotNone(grid(3, height=5))
        self.assertIsNone(grid(3, height=4))
        self.assertIsNone(grid([slot("%1", slot("%2"), slot("%3"))], height=2))
        self.assertIsNone(grid(1, manager_width=0))

    def test_root_is_the_manager_then_the_columns(self):
        root = grid(4)
        self.assertEqual([c.kind for c in (root, *root.children)],
                         [tui_claude.LEFT_RIGHT, tui_claude.LEAF, tui_claude.LEFT_RIGHT])
        self.assertEqual([c.kind for c in root.children[1].children], [tui_claude.TOP_BOTTOM, tui_claude.LEAF])

    def test_sub_workers_right_half_equal_heights(self):
        root = grid([slot("%1"), slot("%2", slot("%3"), slot("%4")), slot("%5")])
        self.assertEqual(body(root), "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17{40x15,80,17,2,"
                                     "39x15,121,17[39x7,121,17,3,39x7,121,25,4]},80x15,80,33,5]}")
        self.tiles(root)

    def test_grandchild_halves_the_childs_cell(self):
        root = grid([slot("%1"), slot("%2", slot("%3", slot("%6")), slot("%4")), slot("%5")])
        self.assertEqual(body(root), "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17{40x15,80,17,2,"
                                     "39x15,121,17[39x7,121,17{19x7,121,17,3,19x7,141,17,6},39x7,121,25,4]},"
                                     "80x15,80,33,5]}")
        self.tiles(root)

    def test_orphan_group_stacks_full_width(self):
        root = grid([slot("%1"), slot(None, slot("%3", slot("%6")), slot("%4")), slot("%5")])
        self.assertEqual(body(root), "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17[80x7,80,17{"
                                     "40x7,80,17,3,39x7,121,17,6},80x7,80,25,4],80x15,80,33,5]}")
        self.tiles(root)

    def test_one_child_or_cell_no_container(self):
        self.assertEqual(body(grid([slot(None, slot("%3"))])), "160x48,0,0{79x48,0,0,0,80x48,80,0,3}")
        self.assertEqual(body(grid([slot("%1", slot("%2"))])),
                         "160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0,1,39x48,121,0,2}}")

    def test_leaves_depth_first(self):
        root = grid([slot("%1"), slot("%2", slot("%3", slot("%6")), slot("%4")), slot("%5"), slot("%7")])
        self.assertEqual([leaf.pane for leaf in tui_claude._leaves(root)],
                         ["%0", "%1", "%2", "%3", "%6", "%4", "%5", "%7"])


class GridTree(unittest.TestCase):
    """Manager %0, its session mgr; rows: (session id, name, @opener, @pane)."""
    MGR = (1, "mgr", "", "")

    def tree(self, rows, panes, layout=None):
        if layout is None:
            layout = grid([slot(p) for p in panes if p != "%0"], per_column=len(panes))
        else:
            layout = tui_claude._parse_layout(layout)
        return tui_claude._grid_tree("%0", panes, [self.MGR, *rows], layout)

    def test_nodes_by_pane_id_then_manual_panes(self):
        rows = [(2, "a", "mgr", "%10"), (3, "b", "mgr", "%2"), (4, "c", "", "%9")]
        self.assertEqual(self.tree(rows, ["%0", "%10", "%5", "%2", "%9", "%3"]),
                         [slot("%2"), slot("%9"), slot("%10"), slot("%3"), slot("%5")])

    def test_a_closed_pane_keeps_the_rest_in_order(self):
        panes = ["%0", "%1", "%3", "%4", "%5", "%6", "%7"]
        rows = [(i + 1, f"w{i}", "mgr", f"%{i}") for i in (1, 3, 4, 5, 6, 7)]
        cells = self.tree(rows, panes)
        self.assertEqual(cells, [slot(p) for p in panes[1:]])
        self.assertEqual([[c.pane for c in col.children] for col in grid(cells).children[1].children],
                         [["%1", "%3", "%4"], ["%5", "%6", "%7"]])

    def test_sub_workers_in_their_parents_cell(self):
        rows = [(2, "w1", "mgr", "%1"), (3, "w2", "mgr", "%2"), (4, "a", "w2", "%3"), (5, "b", "w2", "%4"),
                (6, "w3", "mgr", "%5"), (7, "c", "a", "%6")]
        self.assertEqual(self.tree(rows, ["%0", "%1", "%2", "%3", "%4", "%5", "%6"]),
                         [slot("%1"), slot("%2", slot("%3", slot("%6")), slot("%4")), slot("%5")])

    def test_openers_that_are_no_node_of_the_window(self):
        rows = [(2, "x", "mgr", "%77"), (3, "y", "x", "%1"), (4, "z", "mgr", "%0"), (5, "v", "z", "%2"),
                (6, "u", "", "%3")]
        self.assertEqual(self.tree(rows, ["%0", "%1", "%2", "%3"]), [slot("%1"), slot("%2"), slot("%3")])

    def test_duplicate_pane_claims_the_lower_session_id_owns(self):
        rows = [(5, "x", "mgr", "%1"), (3, "y", "mgr", "%1"), (6, "z", "x", "%2"), (7, "w", "y", "%3")]
        self.assertEqual(self.tree(rows, ["%0", "%1", "%2", "%3"]), [slot("%1", slot("%3")), slot("%2")])

    def test_opener_cycle_goes_main(self):
        rows = [(2, "a", "b", "%1"), (3, "b", "a", "%2"), (4, "c", "a", "%3"), (5, "d", "d", "%4")]
        self.assertEqual(self.tree(rows, ["%0", "%1", "%2", "%3", "%4"]),
                         [slot("%1"), slot("%2"), slot("%3"), slot("%4")])

    def test_an_orphan_group_main_does_not_reach_goes_main_whole(self):
        # a (%1) and lead (%2) open each other; the orphans of gone c1 (%3, %4) sit right of a, or of x (%5, a's)
        cycle = [(2, "a", "lead", "%1"), (3, "lead", "a", "%2"), (4, "k1", "c1", "%3"), (5, "k2", "c1", "%4")]
        for rows, panes, layout, want in (
                (cycle, ["%0", "%1", "%2", "%3", "%4"],
                 "160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0{40x24,80,0,1,39x24,121,0[39x12,121,0,3,39x11,121,13,4]},"
                 "80x23,80,25,2]}",
                 [slot("%1"), slot(None, slot("%3"), slot("%4")), slot("%2")]),
                ([*cycle, (6, "x", "a", "%5")], ["%0", "%1", "%2", "%3", "%4", "%5"],
                 "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0{40x16,80,0,5,39x16,121,0[39x8,121,0,3,39x7,121,9,4]},"
                 "80x15,80,17,1,80x15,80,33,2]}",
                 [slot(None, slot("%3"), slot("%4")), slot("%1"), slot("%2"), slot("%5")])):
            with self.subTest(panes=panes):
                self.assertEqual(self.tree(rows, panes, layout), want)

    def test_orphans_take_the_killed_parents_main_cell(self):
        # w2 (%2, with a %4 and b %5) killed: tmux gave its cell to [a, b]
        layout = "160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17[80x7,80,17,4,80x7,80,25,5],80x15,80,33,3]}"
        rows = [(2, "w1", "mgr", "%1"), (4, "w3", "mgr", "%3"), (5, "a", "w2", "%4"), (6, "b", "w2", "%5")]
        self.assertEqual(self.tree(rows, ["%0", "%1", "%3", "%4", "%5"], layout),
                         [slot("%1"), slot(None, slot("%4"), slot("%5")), slot("%3")])

    def test_orphans_take_the_killed_parents_sub_cell(self):
        # w1 (%1) has children c (%2, killed; its children a %4, b %5) and d (%3)
        layout = ("160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0,1,39x48,121,0[39x24,121,0[39x12,121,0,4,"
                  "39x11,121,13,5],39x23,121,25,3]}}")
        rows = [(2, "w1", "mgr", "%1"), (4, "d", "w1", "%3"), (5, "a", "c", "%4"), (6, "b", "c", "%5")]
        self.assertEqual(self.tree(rows, ["%0", "%1", "%3", "%4", "%5"], layout),
                         [slot("%1", slot(None, slot("%4"), slot("%5")), slot("%3"))])

    def test_orphans_skip_a_manual_panes_cell(self):
        layout = ("160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17{40x15,80,17,9,39x15,121,17[39x7,121,17,4,"
                  "39x7,121,25,5]},80x15,80,33,3]}")
        rows = [(2, "w1", "mgr", "%1"), (4, "w3", "mgr", "%3"), (5, "a", "w2", "%4"), (6, "b", "w2", "%5")]
        self.assertEqual(self.tree(rows, ["%0", "%1", "%3", "%4", "%5", "%9"], layout),
                         [slot("%1"), slot(None, slot("%4"), slot("%5")), slot("%3"), slot("%9")])

    def test_orphans_spanning_main_cells_dissolve(self):
        # the manager's session renamed from old to mgr
        for panes, per_column in ((["%0", "%1", "%2", "%3"], 3), (["%0", "%1", "%2", "%3"], 2), (["%0", "%1"], 3)):
            with self.subTest(panes=panes, per_column=per_column):
                layout = grid([slot(p) for p in panes[1:]], per_column)
                rows = [(int(p[1:]) + 1, f"w{p[1:]}", "old", p) for p in panes[1:]]
                self.assertEqual(self.tree(rows, panes, tui_claude._render_layout(layout)),
                                 [slot(p) for p in panes[1:]])


class Swaps(unittest.TestCase):
    def test_none_when_in_order(self):
        self.assertEqual(tui_claude._swaps(["%0", "%1", "%2"], ["%0", "%1", "%2"]), [])

    def test_example(self):
        self.assertEqual(tui_claude._swaps(["%0", "%1", "%2", "%3"], ["%0", "%3", "%1", "%2"]),
                         [("%3", "%1"), ("%1", "%2")])

    def test_every_order_of_four_correct_and_minimal(self):
        have = ["%0", "%1", "%2", "%3"]
        for want in itertools.permutations(have):
            with self.subTest(want=want):
                swaps = tui_claude._swaps(have, list(want))
                order = list(have)
                for source, target in swaps:
                    i, j = order.index(source), order.index(target)
                    order[i], order[j] = order[j], order[i]
                self.assertEqual(order, list(want))
                seen, cycles = set(), 0
                for start in have:
                    cycles += start not in seen
                    while start not in seen:
                        seen.add(start)
                        start = want[have.index(start)]
                self.assertEqual(len(swaps), len(have) - cycles)


class LayoutString(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((tui_claude.PER_COLUMN, tui_claude.GRID_MANAGER, tui_claude.GRID_PER_COLUMN,
                          tui_claude.GRID_HOOKS),
                         (3, "@grid-manager", "@grid-per-column",
                          ("pane-exited", "window-resized", "window-layout-changed")))
        self.assertTrue(tui_claude.WINDOW.fullmatch("@12"))
        for value in ("@", "%1", "@1a", "1"):
            self.assertFalse(tui_claude.WINDOW.fullmatch(value), value)

    def test_checksum_of_tmux_samples(self):
        for sample in (MAIN_VERTICAL, TILED_SPLIT):
            csum, text = sample.split(",", 1)
            self.assertEqual(tui_claude._checksum(text), csum)

    def test_parse(self):
        Cell, lr, tb = tui_claude.Cell, tui_claude.LEFT_RIGHT, tui_claude.TOP_BOTTOM
        want = Cell(160, 48, 0, 0, kind=lr, children=(
            Cell(80, 48, 0, 0, "%0"),
            Cell(79, 48, 81, 0, kind=tb, children=(Cell(79, 24, 81, 0, "%1"), Cell(79, 23, 81, 25, "%2")))))
        self.assertEqual(tui_claude._parse_layout(MAIN_VERTICAL), want)
        self.assertEqual(tui_claude._parse_layout(MAIN_VERTICAL.split(",", 1)[1]), want)

    def test_round_trip(self):
        for sample in (MAIN_VERTICAL, TILED_SPLIT):
            self.assertEqual(tui_claude._render_layout(tui_claude._parse_layout(sample)), sample)

    def test_malformed(self):
        text = MAIN_VERTICAL.split(",", 1)[1]
        for bad in ("", "7f31", "7f31,", "0000," + text, "7F31," + text, text[:-1], text + "}", text + ",1x1,0,0,9",
                    text.replace("]", "}"), "160x48,0,0", "160x48,0,0{}", "160x48,0,0,%0",
                    "160x48,0,0[1x1,0,0,1x1,0,2,2]", "160x48,0,0,1\n"):
            with self.subTest(bad=bad):
                self.assertIsNone(tui_claude._parse_layout(bad))


class Tile(unittest.TestCase):
    """Window @3, 160x48: manager %0 (session mgr) left, 79 wide; w<i>'s @pane is %<i>."""
    READ = ["tmux", "display-message", "-p", "-t", "@3",
            "#{window_width}\t#{window_height}\t#{window_zoomed_flag}\t#{window_layout}\t#{@grid-manager}\t"
            "#{@grid-per-column}"]
    PANES = ["tmux", "list-panes", "-t", "@3", "-F", "#{pane_id}"]
    TEARDOWN = ["tmux",
                "set-hook", "-u", "-w", "-t", "@3", "pane-exited", ";",
                "set-hook", "-u", "-w", "-t", "@3", "window-resized", ";",
                "set-hook", "-u", "-w", "-t", "@3", "window-layout-changed", ";",
                "set-option", "-u", "-w", "-t", "@3", "@grid-manager", ";",
                "set-option", "-u", "-w", "-t", "@3", "@grid-per-column"]
    THREE = "0a6c,160x48,0,0{79x48,0,0,0,80x48,80,0[80x16,80,0,1,80x15,80,17,2,80x15,80,33,3]}"
    W3 = [("$2", "w1", "mgr", "%1"), ("$3", "w2", "mgr", "%2"), ("$4", "w3", "mgr", "%3")]
    # the manager 100 wide; a sub-worker of w1 below it: tile puts it right of w1
    WIDE = ("8b77,160x48,0,0{100x48,0,0,0,59x48,101,0[59x24,101,0,1,59x23,101,25,2]}", ["%0", "%1", "%2"],
            [("$2", "w1", "mgr", "%1"), ("$3", "a", "w1", "%2")])

    def tile(self, layout, panes, rows=(), manager="%0", n="", zoomed="0", size=(160, 48), fail=(), **kw):
        """tile @3 given what tmux prints; rows: (session id, name, @opener, @pane) besides mgr's. Returns the fake."""
        rows = [("$1", "mgr", "", ""), *rows]
        fake = Tmux(fail=fail, results={
            "display-message": (0, "\t".join((*map(str, size), zoomed, layout, manager, n)) + "\n", ""),
            "list-panes": (0, "".join(f"{p}\n" for p in panes), ""),
            "list-sessions": (0, "".join("\t".join((*row, SOCK)) + "\n" for row in rows), "")})
        tui_claude.tile("@3", proc=fake, **kw)
        return fake

    def test_swaps_then_select_layout_in_one_tmux_command(self):
        # manual pane %4, split from %1, follows it in pane index order; it goes last, in a second column
        layout = "2cc7,160x48,0,0{79x48,0,0,0,80x48,80,0[80x8,80,0,1,80x7,80,9,4,80x15,80,17,2,80x15,80,33,3]}"
        fake = self.tile(layout, ["%0", "%1", "%4", "%2", "%3"], self.W3)
        self.assertEqual(fake.calls, [self.READ, self.PANES, SESSIONS, [
            "tmux", "swap-pane", "-d", "-s", "%2", "-t", "%4", ";", "swap-pane", "-d", "-s", "%3", "-t", "%4", ";",
            "select-layout", "-t", "@3", "ac6e,160x48,0,0{79x48,0,0,0,80x48,80,0{40x48,80,0[40x16,80,0,1,"
                                         "40x15,80,17,2,40x15,80,33,3],39x48,121,0,4}}"]])

    def test_the_managers_width_kept_no_swap_when_in_order(self):
        fake = self.tile(*self.WIDE)
        self.assertEqual(fake.calls[3:], [["tmux", "select-layout", "-t", "@3",
                                           "e37e,160x48,0,0{100x48,0,0,0,59x48,101,0{29x48,101,0,1,29x48,131,0,2}}"]])

    def test_no_op_when_the_layouts_cells_match(self):
        fake = self.tile(self.THREE, ["%0", "%1", "%2", "%3"], self.W3)
        self.assertEqual(fake.calls, [self.READ, self.PANES, SESSIONS])

    def test_zoomed_or_no_grid_window_only_reads_it(self):
        for kw in ({"zoomed": "1"}, {"manager": ""}):
            with self.subTest(**kw):
                fake = self.tile(*self.WIDE, **kw)
                self.assertEqual(fake.calls, [self.READ])

    def test_teardown_when_the_manager_is_gone_invalid_or_alone(self):
        for manager, layout, panes in (
                ("%0", dump("160x48,0,0[160x16,0,0,1,160x15,0,17,2,160x15,0,33,3]"), ["%1", "%2", "%3"]),
                ("0", self.THREE, ["%0", "%1", "%2", "%3"]),
                ("%0;", self.THREE, ["%0", "%1", "%2", "%3"]),
                ("%0", dump("160x48,0,0,0"), ["%0"])):
            with self.subTest(manager=manager, panes=panes):
                fake = self.tile(layout, panes, self.W3, manager=manager)
                self.assertEqual(fake.calls, [self.READ, self.PANES, self.TEARDOWN])

    def test_nothing_changed_when_the_layout_is_unreadable_or_not_the_windows_panes(self):
        four = ["%0", "%1", "%2", "%3"]
        for layout, panes in (("bogus", four), (self.THREE[:-1], four), ("0000" + self.THREE[4:], four),
                              (self.THREE, four[:3]), (self.THREE, [*four, "%4"]), (self.THREE, [*four[:3], "%5"]),
                              (dump("160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0,1,80x23,80,25,1]}"), four[:2])):
            with self.subTest(layout=layout, panes=panes):
                fake = self.tile(layout, panes, self.W3)
                self.assertEqual(fake.calls, [self.READ, self.PANES])

    def test_too_small_nothing_changed(self):
        # three cells side by side, 4 high: a column of three needs 5
        layout = dump("160x4,0,0{79x4,0,0,0,80x4,80,0{26x4,80,0,1,26x4,107,0,2,26x4,134,0,3}}")
        fake = self.tile(layout, ["%0", "%1", "%2", "%3"], self.W3, size=(160, 4))
        self.assertEqual(fake.calls, [self.READ, self.PANES, SESSIONS])

    def test_duplicate_pane_claims_the_lower_session_id_owns(self):
        rows = [("$5", "x", "mgr", "%1"), ("$3", "y", "mgr", "%1"), ("$6", "z", "x", "%2"), ("$7", "w", "y", "%3")]
        fake = self.tile(self.THREE, ["%0", "%1", "%2", "%3"], rows)
        self.assertEqual(fake.calls[3:], [[
            "tmux", "swap-pane", "-d", "-s", "%3", "-t", "%2", ";", "select-layout", "-t", "@3",
            "9a41,160x48,0,0{79x48,0,0,0,80x48,80,0[80x24,80,0{40x24,80,0,1,39x24,121,0,3},80x23,80,25,2]}"]])

    def test_session_fields_read_back_only_after_a_fullmatch(self):
        # a row with a bad id or name is skipped (its pane a manual pane); an @opener that is no opener is unset
        rows = [("$2", "w1", "mgr", "%1"), ("2", "w2", "mgr", "%2"), ("$4", "w 4", "mgr", "%4"),
                ("$5", "w5", "w1", "%3"), ("$6", "w6", "%1", "%5")]
        panes = ["%0", "%1", "%2", "%3", "%4", "%5"]
        fake = self.tile(tui_claude._render_layout(grid([slot(p) for p in panes[1:]], 6)), panes, rows, n="6")
        cells = [slot("%1", slot("%3")), slot("%5"), slot("%2"), slot("%4")]
        self.assertEqual(fake.calls[3][-1], tui_claude._render_layout(grid(cells, 6)))

    def test_per_column_argument_else_the_window_option_else_the_default(self):
        panes = ["%0", "%1", "%2", "%3", "%4"]
        rows = [(f"${i + 1}", f"w{i}", "mgr", f"%{i}") for i in range(1, 5)]
        for n, kw, per_column in (("2", {}, 2), ("9999", {}, 9999), ("1", {"per_column": 2}, 2),
                                  ("2", {"per_column": 3}, None), ("", {}, None), ("0", {}, None), ("02", {}, None),
                                  ("10000", {}, None), ("2 ", {}, None), ("x", {}, None)):
            with self.subTest(n=n, **kw):
                fake = self.tile(tui_claude._render_layout(grid(4)), panes, rows, n=n, **kw)
                want = tui_claude._render_layout(grid(4, per_column)) if per_column else None
                self.assertEqual(fake.calls[3:], [["tmux", "select-layout", "-t", "@3", want]] if want else [])

    def test_bad_window_or_per_column_raises_before_tmux(self):
        cases = [("3", {}, "invalid window id '3': want @[0-9]+"),
                 *((window, {}, "invalid window id") for window in ("@3;", "%3", "@", " @3")),
                 ("@3", {"per_column": 0}, "per_column must be an int from 1 to 9999, not 0"),
                 *(("@3", {"per_column": n}, "per_column") for n in (10000, True, "3", 2.0))]
        for window, kw, error in cases:
            with self.subTest(window=window, **kw):
                fake = Tmux()
                with self.assertRaisesRegex(tui_claude.TuiError, "^" + re.escape(error)):
                    tui_claude.tile(window, proc=fake, **kw)
                self.assertEqual(fake.calls, [])

    def test_a_tmux_failure_raises(self):
        with self.assertRaisesRegex(tui_claude.TuiError, "^tmux: boom$"):
            self.tile(*self.WIDE, fail=("select-layout",))
        with self.assertRaisesRegex(tui_claude.TuiError, "^tmux: boom$"):
            self.tile(dump("160x48,0,0,0"), ["%0"], fail=("set-hook",))
        with self.assertRaisesRegex(tui_claude.TuiError, "^tmux: boom$"):
            tui_claude.tile("@3", proc=Tmux(fail=("display-message",)))


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
                                              template=template, split=split, split_from=split_from, per_column=3)
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

    @unittest.mock.patch.dict(os.environ, {"NO_COLOR": "1"})   # beats FORCE_COLOR, which colors argparse's help (3.14)
    def test_help(self):
        rc, out, _ = self.main("-h")
        self.assertEqual(rc, 0)
        self.assertIn("{start,send,read,show,tile}", out)
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
        show.assert_called_once_with("s", "x", split="below", split_from="b", per_column=3)
        self.main("show", "s")
        self.assertEqual(show.call_args, unittest.mock.call("s", None, split=None, split_from=None, per_column=3))

    @unittest.mock.patch.dict(os.environ, {"NO_COLOR": "1"})
    def test_per_column(self):
        start, show = self.patch("start"), self.patch("show", return_value=None)
        self.patch("status", return_value=tui_claude.RUNNING)
        for value, n in (("1", 1), ("2", 2), ("9999", 9999), ("0003", 3)):
            with self.subTest(value=value):
                self.assertEqual(self.main("start", "--per-column", value, "a", "--", "cmd")[0], 0)
                self.assertEqual(start.call_args.kwargs["per_column"], n)
                self.assertEqual(self.main("show", "--per-column", value, "s")[0], 0)
                self.assertEqual(show.call_args.kwargs["per_column"], n)
        start.reset_mock()
        show.reset_mock()
        for value in ("0", "10000", "00000", "-1", "x", "", "2.0", "\u00b2", " 2"):
            with self.subTest(value=value):
                rc, _, err = self.main("show", "--per-column", value, "s")
                self.assertEqual(rc, 2)
                self.assertIn("--per-column", err)
                self.assertEqual(self.main("start", "--per-column", value, "a", "--", "cmd")[0], 2)
        start.assert_not_called()
        show.assert_not_called()
        for cmd in ("start", "show"):
            self.assertIn("--per-column N", " ".join(self.main(cmd, "-h")[1].split()))

    def test_tile(self):
        real, fake = tui_claude.tile, Tmux()
        tile = self.patch("tile", return_value=None)
        self.assertEqual(self.main("tile", "@3"), (0, "", ""))
        tile.assert_called_once_with("@3")
        tile.side_effect = lambda window: real(window, proc=fake)
        self.assertEqual(self.main("tile", "3"), (1, "", "tui: invalid window id '3': want @[0-9]+\n"))
        self.assertEqual(fake.calls, [])

    def test_exit_1(self):
        for name in ("start", "send", "read", "tile"):
            self.patch(name, side_effect=tui_claude.TuiError("tmux: boom"))
        for argv in ("start a -- cmd", "send s hi", "read s", "tile @3"):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(*argv.split()), (1, "", "tui: tmux: boom\n"))
        status, show = self.patch("status", return_value=None), self.patch("show", return_value="boom")
        self.assertEqual(self.main("show", "s"), (1, "", "tui: no session s\n"))
        show.assert_not_called()
        status.return_value = tui_claude.RUNNING
        self.assertEqual(self.main("show", "s")[0], 1)

    def test_exit_2(self):
        called = [self.patch(n) for n in ("start", "send", "read", "show", "status", "tile")]
        for argv in ((), ("bogus",), ("send", "s"), ("send", "a b", "x"), ("read", "s", "--lines", "0"),
                     ("read", "s", "--lines", "x"), ("read", "s", "--lines", "-1"), ("show", "--split", "up", "s"),
                     ("show", "--split-from", "a b", "s"), ("tile",), ("tile", "@3", "@4")):
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
