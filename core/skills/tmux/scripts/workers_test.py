import contextlib
import errno
import fcntl
import io
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import workers  # noqa: E402
sys.path.insert(0, os.path.join(workers.CORE, "src", "tests"))
import hermetic  # noqa: E402

manager = workers.manager

KW = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}
tui_claude = workers.tui_claude
SID = "0b6f1c2e-3d4a-4b5c-8d6e-7f8091a2b3c4"
RUNNING_PANE, DEAD_PANE = "0  \n", "1 1 \n"
ERROR = "Error: --session-id can only be used with --continue or --resume if --fork-session is also specified."
DEAD_LINE = "Pane is dead (status 1, Wed Oct  7 19:54:08 2026)"
DEAD_TEXT = f"{ERROR}\n" + "\n" * 40 + f"{DEAD_LINE}\n" + "\n" * 10
DEAD_MSG = f"w1: claude exited 1 at once; its pane's last lines:\n{ERROR}\n{DEAD_LINE}"
REFUSED = ("--resume", "-r", "--session-id", "--continue", "-c", "--fork-session", "--from-pr", "--teleport")
WORKERS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workers.py")
UNPRINTABLE = "want printable text (no control character, U+2028 or U+2029)"


def executable(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


def isolate(case):
    """Outside tmux and iTerm2, so the show finds no pane and runs nothing; its stderr lines stay out of the output."""
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "ITERM_SESSION_ID", "TERM_PROGRAM")}
    for p in (mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stderr(io.StringIO())):
        p.__enter__()
        case.addCleanup(p.__exit__, None, None, None)


def no_sleep(case):
    """time.sleep as a recorder, `case.sleep`, for the rest of `case`'s test."""
    p = mock.patch.object(workers.time, "sleep")
    case.sleep = p.start()
    case.addCleanup(p.stop)


@contextlib.contextmanager
def spawned():
    """tui_claude.start as a recorder, yielded, and every session running (tui_claude.status): the rest of
    workers.start runs, on a fake tmux, without a session."""
    with mock.patch.object(tui_claude, "start") as start, \
            mock.patch.object(tui_claude, "status", return_value=tui_claude.RUNNING):
        yield start


def lock_free(path):
    """Whether no one holds the flock on path."""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def decorate_calls(events, status_line=False):
    """The tmux calls tui_claude.decorate makes for worker w1."""
    fake = Fake()
    tui_claude.decorate("w1", events, status_line=status_line, proc=fake)
    return fake.calls


def local(case, text):
    """Core config's local file for the rest of `case`'s test."""
    with open(os.path.join(hermetic.home(case), "core.local.toml"), "w") as f:
        f.write(text)


class Fake:
    """Records proc calls; `new_err` makes new-session fail; `results` maps a tmux command to a function of the argv
    giving its stdout. On new-session or respawn-pane it reads the handover file tui_claude wrote and unlinks it as the
    pane's wrapper would. A session's pane status (display-message), in `panes`, is empty (no session) until
    new-session or respawn-pane makes it `spawned`: a running pane unless a test sets a dead one; capture-pane answers
    `history` (until a clear-history, which fails with `clear_err`) then `text`. Until a respawn-pane, every call first
    adds `late` to `history`: what the old run prints meanwhile. A clear-history stops a sequence (`;`) as it fails."""

    def __init__(self, new_err="", results=None):
        self.new_err, self.results = new_err, results or {}
        self.calls, self.kwargs = [], []
        self.store, self.respawn_rc, self.respawn_err = {}, 0, ""
        self.fail_set, self.oserror, self.handover = None, None, None
        self.panes, self.spawned, self.text, self.history, self.clear_err = {}, RUNNING_PANE, "", "", ""
        self.late = ""

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        self.history += self.late
        if self.oserror and self.oserror(argv):
            raise OSError(2, "No such file or directory")
        if argv[1] in ("set-option", "set-hook") and self.fail_set in argv:
            return subprocess.CompletedProcess(argv, 1, "", "set boom")
        if argv[1] == "set-option" and argv[2] == "-t":
            self.store[argv[4]] = argv[5]
        if argv[1] == "show-options":
            if argv[5] not in self.store:
                return subprocess.CompletedProcess(argv, 1, "", f"invalid option: {argv[5]}")
            return subprocess.CompletedProcess(argv, 0, self.store[argv[5]] + "\n", "")
        if argv[1] == "clear-history":
            if self.clear_err:
                return subprocess.CompletedProcess(argv, 1, "", self.clear_err)
            self.history = ""
        if "respawn-pane" in argv:
            if self.respawn_rc:
                return subprocess.CompletedProcess(argv, self.respawn_rc, "", self.respawn_err)
            with open(argv[-1]) as f:
                self.handover = json.load(f)
            os.unlink(argv[-1])
            self.panes[argv[argv.index("respawn-pane") + 3][1:-1]] = self.spawned
            self.late = ""
        if argv[1] == "new-session":
            path = argv[argv.index(";") - 1]
            with open(path) as f:
                self.handover = json.load(f)
            os.unlink(path)
            if self.new_err:
                return subprocess.CompletedProcess(argv, 1, "", self.new_err)
            self.panes[argv[argv.index("-s") + 1]] = self.spawned
        if argv[1] == "display-message" and argv[-1].startswith("#{pane_dead}"):
            return subprocess.CompletedProcess(argv, 0, self.panes.get(argv[4][1:-1], ""), "")
        if argv[1] == "capture-pane":
            return subprocess.CompletedProcess(argv, 0, self.history + self.text, "")
        if argv[1] in self.results:
            return subprocess.CompletedProcess(argv, 0, self.results[argv[1]](argv), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def respawns(self):
        return [c for c in self.calls if "respawn-pane" in c]

    def new_sessions(self):
        return [c for c in self.calls if c[:2] == ["tmux", "new-session"]]

    def options(self):
        return {c[4]: c[5] for c in self.calls if c[1] == "set-option" and c[2] == "-t"}


class StartTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.realpath(self.tmp.name)
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        self.claude = os.path.join(self.bin, "claude")
        executable(self.claude, "#!/bin/sh\n")
        self.directory = os.path.join(self.dir, "m1")
        manager.ensure(self.directory)
        self.events = os.path.join(self.directory, "events")
        self.env = {"PATH": self.bin, "CLAUDE_CONFIG_DIR": "/cfg", "CLAUDE_FOO": "1", "HOME": "/h",
                    "CLAUDE_JOB_DIR": "/j"}
        for k in workers.STRIP:
            self.env[k] = "x"
        spy = mock.patch.object(tui_claude, "start", wraps=tui_claude.start)
        self.tui = spy.start()
        self.addCleanup(spy.stop)
        no_sleep(self)

    def start(self, fake, name="w1", **kw):
        kw.setdefault("cwd", self.dir)
        kw.setdefault("env", self.env)
        return workers.start(name, self.directory, own=None, proc=fake, **kw)

    def layout(self):
        kw = self.tui.call_args.kwargs
        return kw["split_from"], kw["split"]

    def test_tui_claude_in_process(self):
        self.assertEqual(os.path.realpath(tui_claude.__file__), os.path.join(os.path.realpath(workers.CORE), "src",
                                                                             "tui_claude.py"))
        self.assertTrue(os.path.isfile(os.path.join(workers.CORE, ".claude-plugin", "plugin.json")))

    def test_tui_claude_call(self):
        fake = Fake()
        sid = self.start(fake, prompt="do it", flags=("--model", "m"))
        self.assertEqual(str(uuid.UUID(sid)), sid)
        argv = [self.claude, "--session-id", sid, "--name", "w1", "--model", "m", "--", "do it"]
        env = {k: v for k, v in self.env.items() if k not in workers.STRIP}
        self.tui.assert_called_once_with("w1", argv, cwd=self.dir, env=env, events=self.events, split=None,
                                         split_from=None, per_column=None, status_line=False, proc=fake)
        self.assertEqual(fake.handover["argv"], tui_claude.with_hooks(argv, self.events))

    def test_resume(self):
        fake = Fake()
        sid = self.start(fake, resume=SID, prompt="p", flags=("--model", "m"))
        self.assertEqual(sid, SID)
        self.assertEqual(self.tui.call_args.args[1],
                         [self.claude, "--resume", SID, "--name", "w1", "--model", "m", "--", "p"])
        self.assertEqual(fake.handover["argv"][1:3], ["--settings", tui_claude.hooks(self.events)])
        self.assertEqual(fake.options()["@sid"], SID)

    def test_no_resume_picks_a_new_session_id(self):
        sid = self.start(Fake())
        self.assertEqual(self.tui.call_args.args[1], [self.claude, "--session-id", sid, "--name", "w1"])

    def test_bad_resume(self):
        for bad in ("abc", SID.upper(), "", SID + "x", " " + SID, SID + "\n", SID.replace("-", "")):
            with self.subTest(bad=bad):
                fake = Fake()
                self.assertEqual(self.assert_fails(fake, resume=bad), f"--resume {bad}: not a session id")
                self.assertEqual(fake.calls, [])

    def test_refused_flags(self):
        for resume in (None, SID):
            for flag in REFUSED:
                forms = [(flag, "x")] + ([(f"{flag}=x",)] if flag.startswith("--") else [])
                for given in forms:
                    with self.subTest(resume=resume, given=given):
                        fake = Fake()
                        msg = self.assert_fails(fake, resume=resume, flags=("--model", "m", *given))
                        self.assertEqual(msg, f"{flag}: workers.py picks the session; use start --resume <sid>")
                        self.assertEqual(fake.calls, [])

    def test_flags_after_a_bare_double_dash_are_not_scanned(self):
        flags = ("--model=m", "--", *REFUSED, "--resume=x")
        fake = Fake()
        sid = self.start(fake, flags=flags)
        self.assertEqual(self.tui.call_args.args[1], [self.claude, "--session-id", sid, "--name", "w1", *flags])
        self.assertEqual(json.loads(fake.options()["@flags"]), list(flags))

    def test_no_prompt(self):
        self.start(Fake(), flags=("--x",))
        self.assertEqual(self.tui.call_args.args[1][-1], "--x")

    def test_env_cwd_in_the_handover(self):
        fake = Fake()
        self.start(fake)
        self.assertEqual(fake.handover["cwd"], self.dir)
        env = fake.handover["env"]
        for k in (*workers.STRIP, "CLAUDE_JOB_DIR", "CLAUDE_CODE_CHILD_SESSION"):
            self.assertNotIn(k, env)
        self.assertEqual({k: env[k] for k in ("CLAUDE_FOO", "CLAUDE_CONFIG_DIR", "HOME")},
                         {"CLAUDE_FOO": "1", "CLAUDE_CONFIG_DIR": "/cfg", "HOME": "/h"})
        self.assertIn("CLAUDECODE", self.env)

    def test_pwd_in_the_handover_is_the_cwd(self):
        fake = Fake()
        self.start(fake, env={**self.env, "PWD": "/elsewhere"})
        self.assertEqual(fake.handover["env"]["PWD"], self.dir)

    def test_relative_cwd_made_absolute(self):
        fake = Fake()
        old = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, old)
        self.start(fake, cwd=".")
        self.assertEqual(fake.handover["cwd"], self.dir)
        self.assertEqual(fake.options()["@cwd"], self.dir)

    def test_set_options(self):
        fake = Fake()
        sid = self.start(fake, flags=("--model", "m"), prompt="p")
        opts = fake.options()
        self.assertEqual(set(opts), {"@sid", "@cwd", "@events", "@claude", "@flags", "status"})
        self.assertEqual(opts["@sid"], sid)
        self.assertEqual(opts["@cwd"], self.dir)
        self.assertEqual(opts["@events"], self.events)
        self.assertEqual(opts["@claude"], self.claude)
        self.assertEqual(json.loads(opts["@flags"]), ["--model", "m"])
        for c in fake.calls:
            if c[1] == "set-option" and c[2] == "-t":
                self.assertEqual(c[:4], ["tmux", "set-option", "-t", "=w1:"])
        self.assertLess(fake.calls.index(fake.new_sessions()[0]), min(i for i, c in enumerate(fake.calls)
                                                                    if c[1] == "set-option"))

    def test_decorated_by_tui_claude_before_the_worker_options(self):
        fake = Fake()
        self.start(fake)
        want = decorate_calls(self.events)
        at = fake.calls.index(want[0])
        self.assertEqual(fake.calls[at:at + len(want)], want)
        self.assertEqual(fake.calls[at + len(want)][:5], ["tmux", "set-option", "-t", "=w1:", "@sid"])

    def test_option_failure_kills_session(self):
        for fail in ("@sid", "@flags"):
            fake = Fake()
            fake.fail_set = fail
            with self.assertRaisesRegex(workers.WorkersError, "set boom.*undone", msg=fail):
                self.start(fake)
            self.assertEqual(fake.calls[-1], ["tmux", "kill-session", "-t", "=w1"])

    def test_decoration_failure_is_tui_claudes_to_undo(self):
        for fail in ("@events", "pane-died", "pane-border-format"):
            fake = Fake()
            fake.fail_set = fail
            with self.assertRaisesRegex(workers.WorkersError, "set boom", msg=fail):
                self.start(fake)
            self.assertEqual(fake.calls[-1], ["tmux", "kill-session", "-t", "=w1"])
            self.assertNotIn("@sid", fake.options())

    def test_events_file(self):
        self.start(Fake())
        self.assertEqual(stat.S_IMODE(os.stat(self.events).st_mode), 0o600)

    def test_existing_events_kept(self):
        with open(self.events, "w") as f:
            f.write("old\n")
        os.chmod(self.events, 0o644)
        self.start(Fake())
        with open(self.events) as f:
            self.assertEqual(f.read(), "old\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.events).st_mode), 0o644)

    def assert_fails(self, fake, **kw):
        with self.assertRaises(workers.WorkersError) as cm:
            self.start(fake, **kw)
        self.assertEqual([c for c in fake.calls if len(c) > 1 and c[1] in ("set-option", "set-hook")], [])
        return str(cm.exception)

    def test_bad_name(self):
        fake = Fake()
        self.assert_fails(fake, name="a b")
        self.assertEqual(fake.calls, [])

    def test_directory_missing(self):
        self.directory = os.path.join(self.dir, "nope")
        fake = Fake()
        self.assertEqual(self.assert_fails(fake),
                         f"manager directory {self.directory} not attached: run workers.py attach first")
        self.assertEqual(fake.new_sessions(), [])
        self.assertFalse(os.path.exists(self.directory))

    def test_events_symlink(self):
        target = os.path.join(self.dir, "t")
        open(target, "w").close()
        os.symlink(target, self.events)
        fake = Fake()
        self.assert_fails(fake)
        self.assertEqual(fake.new_sessions(), [])

    def test_claude_not_found(self):
        self.env["PATH"] = os.path.join(self.dir, "empty")
        fake = Fake()
        self.assertIn("claude", self.assert_fails(fake))
        self.assertEqual(fake.new_sessions(), [])

    def test_tui_claude_error_is_a_workers_error(self):
        fake = Fake(new_err="no space for a new session\n")
        self.assertIn("no space for a new session", self.assert_fails(fake))
        self.assertEqual(fake.calls[-1], ["tmux", "kill-session", "-t", "=w1"])

    def test_prompt_after_double_dash(self):
        for prompt in ("-x --model", "plain"):
            fake = Fake()
            self.start(fake, prompt=prompt, flags=("--allowedTools", "Read", "Grep"))
            argv = fake.handover["argv"]
            self.assertEqual(argv[-3:], ["Grep", "--", prompt])
            self.assertEqual(argv.count("--"), 1)

    def test_status_line_from_core_config(self):
        local(self, "status_line = true\n")
        fake = Fake()
        self.start(fake)
        self.assertIs(self.tui.call_args.kwargs["status_line"], True)
        self.assertIn(["tmux", "set-option", "-t", "=w1:", "status", "on"], fake.calls)

    def test_a_bad_status_line_starts_nothing(self):
        local(self, 'status_line = "yes"\n')
        fake = Fake()
        with self.assertRaisesRegex(workers.WorkersError, "core config: status_line: want true or false"):
            self.start(fake)
        self.assertEqual(fake.calls, [])

    def test_per_column_from_core_config(self):
        local(self, "workers_per_column = 2\n")
        self.start(Fake())
        self.assertEqual(self.tui.call_args.kwargs["per_column"], 2)

    def test_a_bad_workers_per_column_starts_nothing(self):
        local(self, "workers_per_column = 0\n")
        fake = Fake()
        with self.assertRaisesRegex(workers.WorkersError, "core config: workers_per_column: want an integer from 1 to 9999"):
            self.start(fake)
        self.assertEqual(fake.calls, [])

    def test_kill_failure_ignored(self):
        fake = Fake()
        fake.fail_set = "@sid"
        fake.oserror = lambda argv: argv[1] == "kill-session"
        with self.assertRaisesRegex(workers.WorkersError, "undone"):
            self.start(fake)

    def test_layout_passed_through(self):
        for kw in ({}, {"split_from": "x"}, {"split": "below"}, {"split_from": "x", "split": "right"}):
            with self.subTest(**kw):
                fake = Fake()
                self.start(fake, **kw)
                self.assertEqual(self.layout(), (kw.get("split_from"), kw.get("split")))
                self.assertNotIn("list-sessions", [c[1] for c in fake.calls if c[-1] != workers.LIVE])

    def test_bad_split_from(self):
        fake = Fake()
        self.assertIn("bad name 'a b'", self.assert_fails(fake, split_from="a b"))
        self.assertEqual(fake.calls, [])

    def test_cwd_missing(self):
        fake = Fake()
        self.assert_fails(fake, cwd=os.path.join(self.dir, "nope"))
        self.assertEqual(fake.new_sessions(), [])

    def test_oserror_from_proc(self):
        for pick in (lambda a: a[1] == "new-session",
                     lambda a: a[1] == "set-option" and "@events" in a, lambda a: a[1] == "set-option" and "@sid" in a):
            fake = Fake()
            fake.oserror = pick
            with self.assertRaises(workers.WorkersError):
                self.start(fake)

    def test_events_not_owned(self):
        fake = Fake()
        with mock.patch.object(workers.os, "getuid", return_value=os.getuid() + 1):
            self.assertIn("owned", self.assert_fails(fake))
        self.assertEqual(fake.new_sessions(), [])

    def test_events_not_regular(self):
        fifo = mock.Mock(st_mode=stat.S_IFIFO | 0o600, st_uid=os.getuid())
        fake = Fake()
        with mock.patch.object(workers.os, "fstat", return_value=fifo):
            self.assertIn("regular", self.assert_fails(fake))
        self.assertEqual(fake.new_sessions(), [])


class WorkerCase(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.realpath(self.tmp.name)
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        self.claude = os.path.join(self.bin, "claude")
        executable(self.claude, "#!/bin/sh\n")
        self.directory = os.path.join(self.dir, "it's m1")
        manager.ensure(self.directory)
        self.events = os.path.join(self.directory, "events")
        self.env = {"PATH": self.bin + ":/a b", "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "cfg")}
        no_sleep(self)

    def started(self, fake, flags=("--model", "m c")):
        return workers.start("w1", self.directory, own=None, cwd=self.dir, flags=flags, env=self.env, proc=fake)


class ContainerTest(WorkerCase):
    def test_workers_stack_in_tmux_splits(self):
        """In a Linux container: the commander in tmux session cmd, pane %0, shown on /dev/pts/1; no pgrep or iTerm2
        variables."""
        sock = "/tmp/tmux-0/default"
        tmux = os.path.join(self.bin, "tmux")
        executable(tmux, "#!/bin/sh\n")
        new = iter(["%5\n", "%6\n"])

        def sessions(argv):
            shown = [(c[3][1:-1], c[5]) for c in fake.calls if c[1:3] == ["set-option", "-t"] and c[4] == "@pane"]
            return "".join(f"${i}\t{name}\tcmd\t{pane}\t{sock}\n" for i, (name, pane) in enumerate(shown, 1))
        fake = Fake(results={"display-message": lambda argv: "cmd\n", "list-sessions": sessions,
                             "list-clients": lambda argv: f"100 /dev/pts/1 %0 0 {sock}\n",
                             "list-panes": lambda argv: "%0\n%5\n" if argv[-1] == "#{pane_id}" else "0 /dev/pts/0 %0\n",
                             "split-window": lambda argv: next(new)})
        fake.oserror = lambda argv: argv[0] == "pgrep"
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"TMUX": f"{sock},42,0", "TMUX_PANE": "%0", "PATH": self.bin}, clear=True), \
                contextlib.redirect_stderr(err):
            for name in ("w1", "w2"):
                workers.start(name, self.directory, own=None, cwd=self.dir, env=self.env, proc=fake)
        self.assertEqual([c for c in fake.calls if c[1] == "split-window"], [
            ["tmux", "split-window", "-d", flag, "-P", "-F", "#{pane_id}", "-t", pane,
             "env", "-u", "TMUX", tmux, "-S", sock, "attach", "-t", f"={name}"]
            for flag, pane, name in (("-h", "%0", "w1"), ("-v", "%5", "w2"))])
        self.assertNotIn("osascript", [c[0] for c in fake.calls])
        self.assertEqual(err.getvalue(), "tui: session w1: tmux attach -t '=w1'\ntui: session w2: tmux attach -t '=w2'\n")


def status_call():
    """The tmux call tui_claude.status makes for worker w1."""
    fake = Fake()
    tui_claude.status("w1", proc=fake)
    return fake.calls[0]


class EarlyDeathTest(WorkerCase):
    RUNS = ("start", "restart")

    def interleaved(self, fake):
        """Logs each sleep among fake's calls."""
        self.sleep.side_effect = lambda secs: fake.calls.append(["sleep", secs])

    def dead(self, fake, text=DEAD_TEXT, pane=DEAD_PANE):
        fake.spawned, fake.text = pane, text

    def attempt(self, run, fake, setup):
        """Runs `run` (the start or the restart of w1) after setup(fake), and returns the WorkersError it raises.
        A restart follows a start that finds a running pane."""
        self.sleep.reset_mock(side_effect=True)
        if run == "restart":
            self.started(fake)
            self.sleep.reset_mock()
        setup(fake)
        with self.assertRaises(workers.WorkersError) as cm:
            self.started(fake) if run == "start" else workers.restart("w1", env=self.env, proc=fake)
        return str(cm.exception)

    def capture(self):
        return ["tmux", "capture-pane", "-p", "-J", "-t", "=w1:", "-S", "-2000"]

    def test_start_watches_a_running_pane_for_5_s(self):
        fake = Fake()
        self.interleaved(fake)
        self.started(fake)
        last = max(i for i, c in enumerate(fake.calls) if c[1] == "set-option")
        self.assertEqual(fake.calls[last][:5], ["tmux", "set-option", "-t", "=w1:", "@flags"])
        self.assertEqual(fake.calls[last + 1:],
                         [["tmux", "list-sessions", "-F", workers.LIVE], *[["sleep", 0.5], status_call()] * 10])
        self.assertEqual(self.sleep.call_args_list, [mock.call(0.5)] * 10)

    def test_restart_watches_a_running_pane_for_5_s(self):
        fake = Fake()
        self.started(fake)
        self.sleep.reset_mock()
        self.interleaved(fake)
        workers.restart("w1", env=self.env, proc=fake)
        at = max(i for i, c in enumerate(fake.calls) if "respawn-pane" in c)
        self.assertEqual(fake.calls[at + 1:], [["sleep", 0.5], status_call()] * 10)
        self.assertEqual(self.sleep.call_args_list, [mock.call(0.5)] * 10)

    def test_dead_pane(self):
        for run in self.RUNS:
            with self.subTest(run=run):
                fake = Fake()
                self.assertEqual(self.attempt(run, fake, self.dead), DEAD_MSG)
                self.assertEqual(self.sleep.call_args_list[-1:], [mock.call(0.5)])
                self.assertEqual(fake.calls[-1], self.capture())
                self.assertNotIn("kill-session", [c[1] for c in fake.calls])

    def test_death_reported_at_the_first_dead_check(self):
        for run in self.RUNS:
            with self.subTest(run=run):
                fake = Fake()

                def setup(fake):
                    def die(secs):
                        if self.sleep.call_count == 3:
                            fake.panes["w1"] = DEAD_PANE
                    self.sleep.side_effect = die
                    fake.text = DEAD_TEXT
                self.assertEqual(self.attempt(run, fake, setup), DEAD_MSG)
                self.assertEqual(self.sleep.call_args_list, [mock.call(0.5)] * 3)

    def test_pane_text_blank_lines_dropped_last_20_kept(self):
        old = [f"old {i}" for i in range(7)]
        kept = [f"kept {i}" for i in range(20)]
        text = "".join(f"{line}\n\n  \n" for line in (*old, *kept)) + "\n\n"
        for run in self.RUNS:
            with self.subTest(run=run):
                msg = self.attempt(run, Fake(), lambda fake: self.dead(fake, text))
                self.assertEqual(msg, "w1: claude exited 1 at once; its pane's last lines:\n" + "\n".join(kept))

    def test_fewer_than_20_lines(self):
        msg = self.attempt("start", Fake(), lambda fake: self.dead(fake, "\n\none\n\n\ntwo\n\n"))
        self.assertEqual(msg, "w1: claude exited 1 at once; its pane's last lines:\none\ntwo")

    def test_restart_report_shows_no_earlier_run(self):
        def setup(fake):
            fake.history, fake.late = "an earlier run's line\n", "a line the earlier run printed late\n"
            self.dead(fake)
        self.assertEqual(self.attempt("restart", Fake(), setup), DEAD_MSG)

    def test_signal_status(self):
        msg = self.attempt("start", Fake(), lambda fake: self.dead(fake, pane="1  9\n"))
        self.assertRegex(msg, r"^w1: claude exited 137 at once; ")

    def test_gone(self):
        for run in self.RUNS:
            with self.subTest(run=run):
                msg = self.attempt(run, Fake(), lambda fake: setattr(fake, "spawned", ""))
                self.assertEqual(msg, "w1: session ended at once")
                self.assertEqual(self.sleep.call_args_list[-1:], [mock.call(0.5)])

    def test_status_failure(self):
        def setup(fake):
            fake.oserror = lambda argv: argv[1] == "display-message" and "w1" in fake.panes
        for run in self.RUNS:
            with self.subTest(run=run):
                self.assertRegex(self.attempt(run, Fake(), setup), r"^tmux: .*No such file")

    def test_capture_failure(self):
        def setup(fake):
            self.dead(fake)
            fake.oserror = lambda argv: argv[1] == "capture-pane"
        for run in self.RUNS:
            with self.subTest(run=run):
                self.assertRegex(self.attempt(run, Fake(), setup), r"^tmux: .*No such file")

    def test_option_failure_still_undoes_the_start(self):
        fake = Fake()
        fake.fail_set = "@sid"
        self.dead(fake)
        with self.assertRaisesRegex(workers.WorkersError, "set boom; start undone"):
            self.started(fake)
        self.assertEqual(self.sleep.call_args_list, [])
        self.assertEqual(fake.calls[-1], ["tmux", "kill-session", "-t", "=w1"])


class RestartTest(WorkerCase):
    def test_respawns_through_the_handover(self):
        fake = Fake()
        sid = self.started(fake)
        cmd = workers.restart("w1", env=self.env, proc=fake)
        at = max(i for i, c in enumerate(fake.calls) if "respawn-pane" in c)
        respawn = fake.calls[at][fake.calls[at].index("respawn-pane"):-1]
        self.assertEqual(respawn, ["respawn-pane", "-k", "-t", "=w1:", sys.executable, "-I", "-c", tui_claude.EXEC])
        self.assertEqual(fake.kwargs[at]["stdin"], subprocess.DEVNULL)
        resumed = tui_claude.with_hooks([self.claude, "--resume", sid, "--name", "w1", "--model", "m c"], self.events)
        self.assertEqual(resumed[1:3], ["--settings", tui_claude.hooks(self.events)])
        self.assertEqual(fake.handover, {"argv": resumed, "cwd": self.dir, "env": {**self.env, "PWD": self.dir}})
        self.assertEqual(shlex.split(cmd), resumed)

    def test_env_is_the_callers_minus_strip(self):
        fake = Fake()
        self.started(fake)
        caller = {"PATH": self.bin, "FOO": "1", "PWD": "/stale", **{k: "x" for k in workers.STRIP}}
        workers.restart("w1", env=caller, proc=fake)
        self.assertEqual(fake.handover["env"], {"PATH": self.bin, "FOO": "1", "PWD": self.dir})
        for k in ("CLAUDE_JOB_DIR", "CLAUDE_CODE_CHILD_SESSION"):
            self.assertIn(k, workers.STRIP)

    def test_own_settings_merged_with_the_hooks(self):
        own = {"type": "command", "command": "mine"}
        flags = ("--settings", json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [own]}]}}))
        fake = Fake()
        self.started(fake, flags=flags)
        workers.restart("w1", env=self.env, proc=fake)
        words = fake.handover["argv"]
        self.assertEqual(words.count("--settings"), 1)
        settings = json.loads(words[words.index("--settings") + 1])
        ours = json.loads(tui_claude.hooks(self.events))["hooks"]
        self.assertEqual(settings["model"], "x")
        self.assertEqual(settings["hooks"]["Stop"], [{"hooks": [own]}, *ours["Stop"]])
        self.assertEqual(settings["hooks"]["Notification"], ours["Notification"])

    def test_unmergeable_settings_no_respawn(self):
        fake = Fake()
        self.started(fake)
        fake.store["@flags"] = json.dumps(["--settings", "[]"])
        with self.assertRaisesRegex(workers.WorkersError, "--settings"):
            workers.restart("w1", env=self.env, proc=fake)
        self.assertEqual(fake.respawns(), [])

    def test_redecorates_and_clears_state_then_clears_history_in_the_respawns_tmux_command(self):
        fake = Fake()
        self.started(fake)
        n = len(fake.calls)
        workers.restart("w1", env=self.env, proc=fake)
        tail = [c for c in fake.calls[n:] if c[1] not in ("show-options", "display-message")]
        self.assertEqual(tail[:-1], [*decorate_calls(self.events), ["tmux", "set-option", "-t", "=w1:", "@state", ""]])
        self.assertEqual(tail[-1][:6], ["tmux", "clear-history", "-t", "=w1:", ";", "respawn-pane"])

    def test_redecorates_with_the_status_line_core_config_now_has(self):
        fake = Fake()
        self.started(fake)
        local(self, "status_line = true\n")
        n = len(fake.calls)
        workers.restart("w1", env=self.env, proc=fake)
        tail = [c for c in fake.calls[n:] if c[1] not in ("show-options", "display-message")]
        self.assertEqual(tail[:-2], decorate_calls(self.events, status_line=True))

    def test_decoration_failure_no_respawn(self):
        fake = Fake()
        self.started(fake)
        fake.fail_set = "pane-died"
        with self.assertRaisesRegex(workers.WorkersError, "set boom"):
            workers.restart("w1", env=self.env, proc=fake)
        self.assertEqual(fake.respawns(), [])

    def test_clear_history_failure_no_respawn(self):
        fake = Fake()
        self.started(fake)
        fake.clear_err, fake.handover = "can't find pane\n", None
        with self.assertRaisesRegex(workers.WorkersError, "can't find pane"):
            workers.restart("w1", env=self.env, proc=fake)
        self.assertIsNone(fake.handover)

    def test_prints_cmd_to_stderr(self):
        fake = Fake()
        self.started(fake)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cmd = workers.restart("w1", env=self.env, proc=fake)
        self.assertEqual(err.getvalue(), cmd + "\n")

    def test_not_a_worker(self):
        fake = Fake()
        with self.assertRaisesRegex(workers.WorkersError, "w1: not a worker"):
            workers.restart("w1", env=self.env, proc=fake)
        self.assertEqual(fake.respawns(), [])

    def test_corrupt_options_not_a_worker(self):
        for key, bad in (("@flags", ""), ("@flags", "{}"), ("@flags", "[1]"), ("@flags", "null"),
                         ("@sid", ""), ("@claude", ""), ("@cwd", ""), ("@events", "")):
            fake = Fake()
            self.started(fake)
            fake.store[key] = bad
            with self.assertRaisesRegex(workers.WorkersError, "w1: not a worker", msg=f"{key}={bad!r}"):
                workers.restart("w1", env=self.env, proc=fake)
            self.assertEqual(fake.respawns(), [])

    def test_oserror_from_proc(self):
        fake = Fake()
        self.started(fake)
        fake.oserror = lambda argv: "respawn-pane" in argv
        with self.assertRaises(workers.WorkersError):
            workers.restart("w1", env=self.env, proc=fake)

    def test_bad_name(self):
        fake = Fake()
        with self.assertRaises(workers.WorkersError):
            workers.restart("a b", env=self.env, proc=fake)
        self.assertEqual(fake.calls, [])

    def test_respawn_failure(self):
        fake = Fake()
        self.started(fake)
        fake.respawn_rc, fake.respawn_err = 1, "can't find pane\n"
        with self.assertRaisesRegex(workers.WorkersError, "can't find pane"):
            workers.restart("w1", env=self.env, proc=fake)


def line(kind, *blocks, mid=None):
    message = {"content": list(blocks)}
    if mid:
        message["id"] = mid
    return json.dumps({"type": kind, "message": message}, ensure_ascii=False) + "\n"


def text(t):
    return {"type": "text", "text": t}


TOOL = {"type": "tool_use", "id": "x", "name": "Bash", "input": {}}


class ReplyTest(WorkerCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.env["CLAUDE_CONFIG_DIR"]

    def transcript(self, *lines, sid=SID, project="-p"):
        d = os.path.join(self.cfg, "projects", project)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".jsonl"), "w") as f:
            f.write("".join(lines))

    def fake(self, sid=SID):
        fake = Fake()
        fake.store["@sid"] = sid
        return fake

    def reply(self, fake=None, **kw):
        return workers.reply("w1", proc=fake or self.fake(), config=self.cfg, **kw)

    def test_last_assistant_text_wins(self):
        self.transcript(line("assistant", text("first")), line("user", text("q")),
                        line("assistant", text("a"), TOOL, text("b")), line("assistant", TOOL),
                        "not json\n", line("user", text("later")))
        self.assertEqual(self.reply(), "a\nb")

    def test_blocks_of_one_message_joined(self):
        self.transcript(line("assistant", text("old"), mid="m0"), line("assistant", text("a"), mid="m1"),
                        line("assistant", TOOL, mid="m1"), line("assistant", text("b"), mid="m1"),
                        line("assistant", TOOL, mid="m2"))
        self.assertEqual(self.reply(), "a\nb")

    def test_lines_without_id_are_own_messages(self):
        self.transcript(line("assistant", text("a")), line("assistant", text("b")))
        self.assertEqual(self.reply(), "b")

    def test_other_records_inside_a_message_skipped(self):
        self.transcript(line("assistant", text("old"), mid="m0"), line("assistant", text("a"), mid="m1"),
                        line("user", text("result")), "not json\n", line("assistant", text("b"), mid="m1"))
        self.assertEqual(self.reply(), "a\nb")

    def test_long_transcript_read_from_its_end(self):
        """The last message's records span several reads, one starting inside a UTF-8 character; the reads stop at
        the record before that message."""
        big = "\u00e9" * 40000
        earlier = [line("assistant", text(f"old {i} " + "x" * 1000), mid=f"m{i}") for i in range(300)]
        last = [line("assistant", text("a " + big), mid="mL"), line("assistant", TOOL, mid="mL"),
                line("assistant", text("b " + big), mid="mL"), line("assistant", text("c " + big), mid="mL")]
        tail = [line("user", text("q")), line("assistant", TOOL, mid="mN")]
        data = "".join([*earlier, *last, *tail]).encode()
        if data[len(data) - workers.BLOCK] & 0xC0 != 0x80:
            tail[0] = line("user", text("qq"))
            data = "".join([*earlier, *last, *tail]).encode()
        self.assertEqual(data[len(data) - workers.BLOCK] & 0xC0, 0x80)
        self.transcript(*earlier, *last, *tail)
        reads = []

        def spy(*args, **kw):
            f = open(*args, **kw)
            read = f.read
            f.read = lambda n=-1: reads.append(read(n)) or reads[-1]
            return f
        with mock.patch.object(workers, "open", side_effect=spy, create=True):
            self.assertEqual(self.reply(), "\n".join(("a " + big, "b " + big, "c " + big)))
        self.assertGreater(len(reads), 3)
        self.assertLessEqual(sum(map(len, reads)), len("".join([*last, *tail]).encode()) + workers.BLOCK)

    def test_a_partial_last_line(self):
        for last, want in ((line("assistant", text("cut"), mid="m2")[:-10], "a"),
                           (line("assistant", text("whole"), mid="m2").rstrip("\n"), "whole")):
            with self.subTest(want=want):
                self.transcript(line("assistant", text("a"), mid="m1"), last)
                self.assertEqual(self.reply(), want)

    def test_no_assistant_text(self):
        for lines in ((), ("\n", "\n"), (line("user", text("q")), line("assistant", TOOL, mid="m1"), "x")):
            with self.subTest(lines=lines):
                self.transcript(*lines)
                self.assertEqual(self.reply(), "")

    def test_bad_name(self):
        fake = self.fake()
        with self.assertRaises(workers.WorkersError):
            workers.reply("a b", proc=fake, config=self.cfg)
        self.assertEqual(fake.calls, [])

    def test_none_yet(self):
        self.transcript(line("user", text("q")), line("assistant", TOOL))
        self.assertEqual(self.reply(), "")

    def test_no_transcript(self):
        with self.assertRaises(workers.WorkersError) as cm:
            self.reply()
        self.assertIn(f"no transcript for {SID}", str(cm.exception))

    def test_other_session_not_used(self):
        self.transcript(line("assistant", text("x")), sid="1" + SID[1:])
        with self.assertRaises(workers.WorkersError):
            self.reply()

    def test_bad_sid_before_glob(self):
        self.transcript(line("assistant", text("x")), sid="evil")
        with mock.patch.object(workers.glob, "glob") as g:
            with self.assertRaisesRegex(workers.WorkersError, "bad session id"):
                self.reply(self.fake("*"))
            g.assert_not_called()

    def test_not_a_worker(self):
        with self.assertRaisesRegex(workers.WorkersError, "w1: not a worker"):
            self.reply(Fake())

    def test_config_from_env(self):
        self.transcript(line("assistant", text("hi")))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg}):
            self.assertEqual(workers.reply("w1", proc=self.fake()), "hi")

    def test_config_default_home(self):
        home = os.path.join(self.dir, "home")
        d = os.path.join(home, ".claude", "projects", "p")
        os.makedirs(d)
        with open(os.path.join(d, SID + ".jsonl"), "w") as f:
            f.write(line("assistant", text("home")))
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        env["HOME"] = home
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(workers.reply("w1", proc=self.fake()), "home")


class MainTest(WorkerCase):
    def setUp(self):
        super().setUp()
        self.agent_pm = hermetic.home(self)
        self.default = os.path.join(self.agent_pm, "managers", "m1", "events")
        attached("--manager", "m1")

    def run_main(self, argv, fake=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(workers.subprocess, "run", fake or Fake()), \
                mock.patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = workers.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def entries(self):
        with open(os.path.join(os.path.dirname(self.default), "roster.json")) as f:
            return json.load(f)["entries"]

    def test_start(self):
        fake = Fake()
        rc, out, err = self.run_main(["start", "w1", "--manager", "m1", "--cwd", self.dir, "--prompt", "go",
                                      "--note", "check it", "--", "--model", "sonnet", "--permission-mode", "default"],
                                     fake)
        self.assertEqual(rc, 0, err)
        sid = fake.store["@sid"]
        self.assertEqual(out, f"w1 {sid}\n")
        self.assertEqual(json.loads(fake.store["@flags"]), ["--model", "sonnet", "--permission-mode", "default"])
        self.assertEqual(fake.handover["argv"][-1], "go")
        self.assertEqual(fake.store["@events"], self.default)
        e = self.entries()["w1"]
        self.assertEqual((e["kind"], e["sid"], e["note"]), ("worker", sid, "check it"))

    def test_start_an_entry_write_failure_one_stderr_line_exit_0(self):
        fake = Fake()
        flags = ["--append-system-prompt", "a\nb"]
        rc, out, err = self.run_main(["start", "w1", "--manager", "m1", "--cwd", self.dir, "--", *flags], fake)
        sid = fake.store["@sid"]
        resume = [sys.executable, WORKERS, "start", "w1", "--manager", "m1", "--resume", sid, "--cwd", self.dir,
                  "--", *flags]
        self.assertEqual((rc, out), (0, f"w1 {sid}\n"))
        self.assertEqual([x for x in err.splitlines() if not x.startswith("tui: ")],
                         [f"manager: entry w1 not written: entry: resume: {resume!r}: want a list of 2 or more "
                          "printable strings"])
        self.assertEqual(self.entries(), {})
        self.assertNotIn("kill-session", [c[1] for c in fake.calls])

    def test_start_resume(self):
        fake = Fake()
        rc, out, err = self.run_main(["start", "w1", "--manager", "m1", "--cwd", self.dir, "--resume", SID,
                                      "--", "--model", "m"], fake)
        self.assertEqual((rc, out), (0, f"w1 {SID}\n"), err)
        self.assertEqual(fake.store["@sid"], SID)
        self.assertEqual(fake.handover["argv"], tui_claude.with_hooks(
            [self.claude, "--resume", SID, "--name", "w1", "--model", "m"], self.default))

    def test_start_early_death_exit_1(self):
        fake = Fake()
        fake.spawned, fake.text = DEAD_PANE, DEAD_TEXT
        rc, out, err = self.run_main(["start", "w1", "--manager", "m1", "--cwd", self.dir], fake)
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.endswith(f"workers: {DEAD_MSG}\n"), err)
        self.assertEqual(err.count("workers:"), 1)

    def test_restart_early_death_exit_1(self):
        fake = Fake()
        self.started(fake)
        fake.spawned, fake.text = DEAD_PANE, DEAD_TEXT
        rc, out, err = self.run_main(["restart", "w1"], fake)
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.endswith(f"workers: {DEAD_MSG}\n"), err)
        self.assertEqual(err.count("workers:"), 1)

    def test_start_refusals_exit_1_before_any_tmux_call(self):
        bad = "a b"
        for argv, msg in ((["--resume", "abc"], "--resume abc: not a session id"),
                          (["--", "--model", "m", "-c"], "-c: workers.py picks the session; use start --resume <sid>"),
                          (["--resume", SID, "--", "--session-id=x"],
                           "--session-id: workers.py picks the session; use start --resume <sid>"),
                          (["--note", "x" * 501], "note: 501 characters: want at most 500"),
                          (["--note", bad], f"note: {bad!r}: {UNPRINTABLE}")):
            with self.subTest(argv=argv):
                fake = Fake()
                self.assertEqual(self.run_main(["start", "w1", "--manager", "m1", *argv], fake),
                                 (1, "", f"workers: {msg}\n"))
                self.assertEqual(fake.calls, [])

    def test_start_help_names_the_resume_option(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            workers.main(["start", "--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertRegex(out.getvalue(), r"--resume SID\s+a session id")

    def test_start_split_from_below_opens_under_its_pane(self):
        sock = "/tmp/tmux-1/default"
        tmux = os.path.join(self.bin, "tmux")
        executable(tmux, "#!/bin/sh\n")
        fake = Fake(results={"list-clients": lambda argv: f"100 /dev/ttys009 %9 0 {sock}\n" if argv[3] == "=x" else "",
                             "split-window": lambda argv: "%10\n"})
        fake.oserror = lambda argv: argv[0] == "pgrep"
        rc, _, err = self.run_main(["start", "w1", "--manager", "m1", "--cwd", self.dir, "--split-from", "x",
                                    "--split", "below", "--", "--model", "m"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual([c for c in fake.calls if c[1] == "split-window"],
                         [["tmux", "split-window", "-d", "-v", "-P", "-F", "#{pane_id}", "-t", "%9",
                           "env", "-u", "TMUX", tmux, "-S", sock, "attach", "-t", "=w1"]])
        self.assertEqual(fake.store["@pane"], "%10")
        self.assertEqual(fake.handover["argv"][-2:], ["--model", "m"])

    def test_start_defaults_cwd(self):
        fake = Fake()
        rc, _, err = self.run_main(["start", "w1", "--manager", "m1"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(fake.store["@cwd"], os.path.realpath(os.getcwd()))
        self.assertEqual(fake.store["@flags"], "[]")

    def test_error_exit(self):
        rc, out, err = self.run_main(["restart", "w1"])
        self.assertEqual((rc, out, err), (1, "", "workers: w1: not a worker\n"))

    def test_restart(self):
        fake = Fake()
        self.started(fake)
        self.env["FOO"] = "the caller's"
        rc, out, err = self.run_main(["restart", "w1"], fake)
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("--resume", err)
        self.assertEqual(fake.handover["env"]["FOO"], "the caller's")

    def test_reply(self):
        fake = Fake()
        fake.store["@sid"] = SID
        d = os.path.join(self.env["CLAUDE_CONFIG_DIR"], "projects", "p")
        os.makedirs(d)
        with open(os.path.join(d, SID + ".jsonl"), "w") as f:
            f.write(line("assistant", text("done")))
        self.assertEqual(self.run_main(["reply", "w1"], fake), (0, "done\n", ""))
        with open(os.path.join(d, SID + ".jsonl"), "w") as f:
            f.write(line("user", text("q")))
        self.assertEqual(self.run_main(["reply", "w1"], fake), (0, "", ""))

    def test_usage_error(self):
        for argv in (["start"], ["start", "w1", "--manager", "m1", "--split", "left"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                workers.main(argv)
            self.assertEqual(cm.exception.code, 2, argv)


class ManagerDirTest(WorkerCase):
    """main's events file is the manager directory's (core/skills/tmux/SKILL.md › Start 1), attached first; start's
    session (spawned) and next_event are fakes, the tmux session of the caller's pane `session` (none: outside tmux)."""
    CMDS = (["start", "w1"], ["next-event", "--after", "0", "--gen", "0"])

    def setUp(self):
        super().setUp()
        self.agent_pm = hermetic.home(self)
        self.managers = os.path.join(self.agent_pm, "managers")

    def events_of(self, name):
        return os.path.join(self.managers, name, "events")

    def run_main(self, argv, session=None):
        """(exit code, stderr, the events file argv's command was given or None)."""
        env = {} if session is None else {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%3"}
        fake = Fake(results={"display-message": lambda argv: f"{session}\n"})
        err = io.StringIO()
        with mock.patch.dict(os.environ, {**self.env, **env}), mock.patch.object(workers.subprocess, "run", fake), \
                spawned() as start, \
                mock.patch.object(workers, "next_event", return_value=(1, "10:00:01 w1 outcome done")) as nxt, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = workers.main(argv)
        if argv[0] == "start":
            return rc, err.getvalue(), start.call_args.kwargs["events"] if start.called else None
        return rc, err.getvalue(), nxt.call_args.args[0] if nxt.called else None

    def test_default_is_the_own_sessions_events(self):
        for cmd, session in zip(self.CMDS, ("m1", "m2")):
            with self.subTest(cmd=cmd[0]):
                attached(own=session)
                self.assertEqual(self.run_main(cmd, session), (0, "", self.events_of(session)))

    def test_manager_overrides_the_own_session(self):
        attached("--manager", "other", own="mgr")
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                self.assertEqual(self.run_main([cmd[0], "--manager", "other", *cmd[1:]], "mgr"),
                                 (0, "", self.events_of("other")))
        self.assertEqual(os.listdir(self.managers), ["other"])

    def test_a_worker_that_starts_workers_uses_its_own_directory(self):
        attached(own="w1")
        self.assertEqual(self.run_main(["start", "w2"], "w1"), (0, "", self.events_of("w1")))

    def test_outside_tmux_without_manager_exit_1(self):
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                self.assertEqual(self.run_main(cmd),
                                 (1, "workers: no manager directory: run inside tmux or give --manager <name>\n", None))
                self.assertEqual(os.listdir(self.agent_pm), [])

    def test_events_option_is_gone_exit_2(self):
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]), mock.patch.object(workers, "start") as start, \
                    mock.patch.object(workers, "next_event") as nxt:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                    workers.main([cmd[0], "--events", os.path.join(self.dir, "e"), *cmd[1:]])
                self.assertEqual(cm.exception.code, 2)
                self.assertFalse(start.called or nxt.called)
                self.assertEqual(os.listdir(self.agent_pm), [])

    def test_a_bad_manager_exit_1(self):
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                self.assertEqual(self.run_main([cmd[0], "--manager", "a/b", *cmd[1:]], "mgr"),
                                 (1, "workers: manager 'a/b': want [A-Za-z0-9_-]+\n", None))

    def test_a_bad_own_session_name_exit_1(self):
        self.assertEqual(self.run_main(["start", "w1"], "a.b"),
                         (1, "workers: own tmux session 'a.b': want [A-Za-z0-9_-]+; give --manager <name>\n", None))

    def test_a_bad_directory_exit_1(self):
        real = os.path.join(self.dir, "managers")
        os.makedirs(os.path.join(real, "m1"), 0o700)
        os.symlink(real, self.managers)
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                self.assertEqual(self.run_main([cmd[0], "--manager", "m1", *cmd[1:]]),
                                 (1, f"workers: manager directory {self.managers}: not a directory owned by you\n",
                                  None))


def nl(*lines):
    return "".join(f"{x}\n" for x in lines)


class NextEventTest(unittest.TestCase):
    A, B, C = "10:00:01 w1 done", "10:00:02 w2 blocked", "10:00:03 w1 outcome needs_input"

    def setUp(self):
        isolate(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(os.path.realpath(self.tmp.name), "events")
        hermetic.home(self)

    def append(self, data, path=None):
        with open(path or self.path, "ab") as f:
            f.write(data if isinstance(data, bytes) else data.encode())

    def sleeping(self, *steps):
        """Patches time.sleep: its nth call runs steps[n]; a call past the last fails the test instead of hanging."""
        calls = iter(steps)

        def sleep(secs):
            step = next(calls, None)
            if step is None:
                raise AssertionError("next_event kept waiting")
            step()
        return mock.patch.object(workers.time, "sleep", side_effect=sleep)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(workers.subprocess, "run", Fake()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = workers.main(["next-event", "--manager", "m1", *argv])
        return rc, out.getvalue(), err.getvalue()

    def error(self, reason, after=0, path=None):
        path = path or self.path
        with self.assertRaises(workers.WorkersError) as cm:
            workers.next_event(path, after)
        self.assertEqual(str(cm.exception), f"events file {path}: {reason}")

    def test_first_event_after_a_line(self):
        self.append(nl(self.A, self.B, self.C))
        with self.sleeping():
            self.assertEqual(workers.next_event(self.path, 1), (2, self.B))
            self.assertEqual(workers.next_event(self.path, 0), (1, self.A))
            self.assertEqual(workers.next_event(self.path, 2), (3, self.C))

    def test_every_kind_is_an_event(self):
        kinds = [f"10:00:0{i} w1 {k}" for i, k in enumerate(("done", "blocked", "dead", "outcome failed", "new x y"))]
        self.append(nl(*kinds))
        with self.sleeping():
            for n, event in enumerate(kinds):
                self.assertEqual(workers.next_event(self.path, n), (n + 1, event))

    def test_after_end_skips_what_is_there(self):
        self.append(nl(self.A, self.B))
        with self.sleeping(lambda: self.append(nl(self.C))) as sleep:
            self.assertEqual(workers.next_event(self.path, None), (3, self.C))
        sleep.assert_called_once_with(0.5)

    def test_after_end_counts_complete_lines_only(self):
        self.append(nl(self.A) + "10:00:02 w1 do")
        with self.sleeping(lambda: self.append("ne\n")):
            self.assertEqual(workers.next_event(self.path, None), (2, "10:00:02 w1 done"))

    def test_after_end_of_a_missing_file_is_zero(self):
        with self.sleeping(lambda: self.append(nl(self.A))):
            self.assertEqual(workers.next_event(self.path, None), (1, self.A))

    def test_event_appended_while_waiting(self):
        self.append(nl(self.A))
        with self.sleeping(lambda: self.append(nl(self.B))) as sleep:
            self.assertEqual(workers.next_event(self.path, 1), (2, self.B))
        sleep.assert_called_once_with(0.5)

    def test_fewer_lines_than_after_waits_for_the_line_after(self):
        self.append(nl(self.A))
        with self.sleeping(lambda: self.append(nl(self.B)), lambda: self.append(nl(self.C))) as sleep:
            self.assertEqual(workers.next_event(self.path, 2), (3, self.C))
        self.assertEqual(sleep.call_count, 2)

    def test_malformed_lines_skipped_but_counted(self):
        junk = ["", "garbage", "1:00:01 w1 done", "10:00:01 w1", "10:00:01 w1 ", "10:00:01  w1 done", " 10:00:01 w1 done"]
        self.append(nl(self.A, *junk, self.B))
        with self.sleeping():
            self.assertEqual(workers.next_event(self.path, 1), (len(junk) + 2, self.B))

    def test_decoded_as_utf8_with_replacement(self):
        self.append(b"10:00:01 w1 caf\xc3\xa9 \xff\n")
        with self.sleeping():
            self.assertEqual(workers.next_event(self.path, 0), (1, "10:00:01 w1 caf\u00e9 \ufffd"))

    def test_partial_last_line_waits_for_its_newline(self):
        self.append(nl(self.A) + "10:00:02 w1 caf")
        with self.sleeping(lambda: self.append(b"\xc3"), lambda: self.append(b"\xa9\n")) as sleep:
            self.assertEqual(workers.next_event(self.path, 1), (2, "10:00:02 w1 caf\u00e9"))
        self.assertEqual(sleep.call_count, 2)

    def test_missing_file_waited_for(self):
        with self.sleeping(lambda: None, lambda: self.append(nl(self.A))) as sleep:
            self.assertEqual(workers.next_event(self.path, 0), (1, self.A))
        self.assertEqual(sleep.call_count, 2)

    def test_consumed_bytes_not_read_again(self):
        consumed = "0123456789abcdef\n"
        self.append(consumed)

        def rewrite():
            with open(self.path, "r+b") as f:
                f.write(b"\n" * len(consumed))
        with self.sleeping(rewrite, lambda: self.append(nl(self.B))):
            self.assertEqual(workers.next_event(self.path, 0), (2, self.B))

    def test_shrunk_file(self):
        self.append(nl(self.A, self.B))

        def shrink():
            with open(self.path, "wb") as f:
                f.write(nl(self.A).encode())
        with self.sleeping(shrink):
            self.error("shrank", 2)

    def test_replaced_file(self):
        self.append(nl(self.A, self.B))

        def replace():
            with open(self.path + ".new", "wb") as f:
                f.write(nl(self.B, self.A).encode())
            os.replace(self.path + ".new", self.path)
        with self.sleeping(replace):
            self.error("replaced", 2)

    def test_unreadable_path(self):
        with self.sleeping():
            self.error(os.strerror(errno.EISDIR), path=self.tmp.name)

    def test_main_prints_line_and_event(self):
        attached("--manager", "m1")
        path = manager.events(manager.directory("m1"), create=False)
        self.append(nl(self.A, self.B), path)
        with self.sleeping():
            self.assertEqual(self.run_main("--after", "1", "--gen", "0"), (0, f"2 {self.B}\n", ""))
        with self.sleeping(lambda: self.append(nl(self.C), path)):
            self.assertEqual(self.run_main("--after", "end", "--gen", "0"), (0, f"3 {self.C}\n", ""))

    def test_main_error_exit(self):
        attached("--manager", "m1")
        err = workers.WorkersError(f"events file {self.path}: {os.strerror(errno.EISDIR)}")
        with mock.patch.object(workers, "next_event", side_effect=err):
            self.assertEqual(self.run_main("--after", "0", "--gen", "0"), (1, "", f"workers: {err}\n"))

    def test_main_usage_error(self):
        for argv in (*(["--after", bad, "--gen", "0"] for bad in ("-1", "x", "1.5", "")),
                     *(["--after", "1", "--gen", bad] for bad in ("-1", "x", "end", "")),
                     ["--after", "1"], ["--gen", "0"], []):
            with self.sleeping(), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                workers.main(["next-event", "--manager", "m1", *argv])
            self.assertEqual(cm.exception.code, 2, argv)


class Gone(Fake):
    """No session: show-options and capture-pane fail as tmux does."""

    def __call__(self, argv, **kw):
        if argv[1] == "capture-pane":
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 1, "", "can't find session: w1\n")
        return super().__call__(argv, **kw)


class NextEventContentTest(WorkerCase):
    """What next-event prints after `<line> <event>`."""
    PANE = [f"pane {i}" for i in range(50)]
    CAPTURE = ["tmux", "capture-pane", "-p", "-J", "-t", "=w1:", "-S", "-40"]

    def setUp(self):
        super().setUp()
        hermetic.home(self)
        attached("--manager", "m1")

    def run_event(self, event, fake, *transcript):
        """main's (exit code, stdout, stderr) for next-event on an events file holding only `event`; transcript: the
        lines of session SID's transcript, if any."""
        if transcript:
            d = os.path.join(self.env["CLAUDE_CONFIG_DIR"], "projects", "-p")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, SID + ".jsonl"), "w") as f:
                f.write("".join(transcript))
        with open(manager.events(manager.directory("m1"), create=False), "w") as f:
            f.write(event + "\n")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(workers.subprocess, "run", fake), mock.patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = workers.main(["next-event", "--manager", "m1", "--after", "0", "--gen", "0"])
        return rc, out.getvalue(), err.getvalue()

    def pane(self, worker=True, fake=None):
        """A fake tmux whose w1 pane shows PANE; a worker's session holds @sid SID."""
        fake = fake or Fake()
        fake.text = nl(*self.PANE)
        if worker:
            fake.store["@sid"] = SID
        return fake

    def test_worker_done_prints_the_first_80_lines_of_its_reply(self):
        reply = [f"r{i}" for i in range(100)]
        fake = self.pane()
        got = self.run_event("10:00:01 w1 done", fake, line("assistant", text("\n".join(reply)), mid="m1"))
        self.assertEqual(got, (0, nl("1 10:00:01 w1 done", *reply[:80]), ""))
        self.assertNotIn(self.CAPTURE, fake.calls)

    def test_role_run_done_prints_the_panes_last_40_lines(self):
        fake = self.pane(worker=False)
        self.assertEqual(self.run_event("10:00:01 w1 done", fake), (0, nl("1 10:00:01 w1 done", *self.PANE[10:]), ""))
        self.assertEqual(fake.calls[-1], self.CAPTURE)

    def test_blocked_and_dead_print_the_panes_last_40_lines(self):
        for kind in ("blocked", "dead"):
            with self.subTest(kind=kind):
                fake = self.pane()
                got = self.run_event(f"10:00:01 w1 {kind}", fake, line("assistant", text("reply"), mid="m1"))
                self.assertEqual(got, (0, nl(f"1 10:00:01 w1 {kind}", *self.PANE[10:]), ""))
                self.assertEqual([c[1] for c in fake.calls], ["capture-pane"])

    def test_other_events_print_nothing_more(self):
        for event in ("10:00:01 w1-drive outcome done", "10:00:01 w1 new kind"):
            with self.subTest(event=event):
                fake = self.pane()
                self.assertEqual(self.run_event(event, fake), (0, nl(f"1 {event}"), ""))
                self.assertEqual(fake.calls, [])

    def test_empty_reply_prints_nothing_more(self):
        fake = self.pane()
        got = self.run_event("10:00:01 w1 done", fake, line("user", text("q")), line("assistant", TOOL, mid="m1"))
        self.assertEqual(got, (0, nl("1 10:00:01 w1 done"), ""))
        self.assertNotIn(self.CAPTURE, fake.calls)

    def test_a_content_failure_is_printed_on_stdout_exit_0(self):
        no_transcript = f"workers: no transcript for {SID}: started with a leaked Claude Code variable?"
        gone = "workers: tmux: can't find session: w1"
        for event, fake, msg in (("10:00:01 w1 done", self.pane(), no_transcript),
                                 ("10:00:01 w1 done", self.pane(False, Gone()), gone),
                                 ("10:00:01 w1 blocked", self.pane(False, Gone()), gone),
                                 ("10:00:01 a.b dead", self.pane(), "workers: invalid session name 'a.b': want "
                                                                    "[A-Za-z0-9_-]+")):
            with self.subTest(event=event, msg=msg):
                self.assertEqual(self.run_event(event, fake), (0, nl(f"1 {event}", msg), ""))


WHEN = "2026-10-09T12:00:00+00:00"
SID2 = "1" + SID[1:]
PY = "/usr/bin/python3"
SCRIPT = {"worker": "/x/workers.py", "role": "/x/drive.py", "pipeline": "/x/router.py"}
HEADER = "name\tkind\tsid\tstate\tnote\tcwd\tpane\n"
SHOWN = "1 /dev/ttys001\n"
SHOW = tui_claude.show
WIDTH = 64
SPAN = manager.EVENTS_MAX // WIDTH


def numbered(count, start=1):
    """count event lines `12:00:00 w<i> done`, i from start, dot-padded to WIDTH bytes with their newline."""
    return nl(*(f"12:00:00 w{i} done ".ljust(WIDTH - 1, ".") for i in range(start, start + count)))


def tail(directory, n=0, gen=0, *backlog):
    """What attach prints after the table for manager directory `directory`: the backlog, N and GEN, the arm
    commands."""
    me, events = shlex.quote(os.path.abspath(workers.__file__)), shlex.quote(os.path.join(directory, "events"))
    name = os.path.basename(directory)
    return nl(*backlog, f"N={n} GEN={gen}", f"monitor: tail -n +{n + 1} -F {events}",
              f"monitor expired: python3 {me} attach --manager {name} --after LINE --gen {gen} --resume",
              f"next-event: python3 {me} next-event --manager {name} --after {n} --gen {gen}")


def entry(kind="worker", **over):
    """A valid roster entry."""
    return {"kind": kind, "sid": SID, "cwd": "/w", "resume": [PY, SCRIPT[kind], "--go"], "note": None, "opener": None,
            "pane": None, "split": None, "split_from": None, "tui": None, "state": "working", "started": WHEN, **over}


def opts(sid="", state="", pane="", opener=""):
    """A live session's @sid, @state, @pane and @opener."""
    return (sid, state, pane, opener)


def result(**fields):
    """A run.jsonl line of kind result."""
    return json.dumps({"ts": "2026-10-09T12:00:00", "kind": "result", **fields}) + "\n"


@contextlib.contextmanager
def within(seconds=10):
    def fire(*_):
        raise AssertionError("blocked")

    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


class Server:
    """A fake tmux server for the roster commands. `sessions` maps a session to its opts(); `extra` is appended to
    list-sessions' rows; `clients` maps a session to its list-clients output; `panes` is list-panes -a's; `fail` maps a
    command, or a command with its target (`list-clients -t =w1`), to the stderr it fails with (an OSError: raised);
    kill-session ends a session. The caller's pane is in session `own` (None: outside tmux). Any other program is a
    recovery command: recorded in `recoveries` as (argv, its keywords, whether roster.lock at `lock` was free), it
    exits as `results` maps its argv[1] ((code, output), or an exception raised; default (0, ""))."""

    def __init__(self, own="mgr", sessions=None, clients=None, panes="", fail=None, extra="", results=None, lock=None):
        self.own, self.clients, self.panes, self.fail, self.extra = own, clients or {}, panes, fail or {}, extra
        self.sessions = {**({own: opts()} if own else {}), **(sessions or {})}
        self.results, self.lock = results or {}, lock
        self.calls, self.recoveries = [], []

    def __call__(self, argv, **kw):
        if argv[0] != "tmux":
            self.recoveries.append((argv, kw, self.lock is None or lock_free(self.lock)))
            result = self.results.get(argv[1], (0, ""))
            if isinstance(result, Exception):
                raise result
            return subprocess.CompletedProcess(argv, result[0], result[1])
        self.calls.append(argv)
        cmd, out, rc = argv[1], "", 0
        fail = self.fail.get(cmd, self.fail.get(" ".join(argv[1:4])))
        if isinstance(fail, OSError):
            raise fail
        if fail is not None:
            return subprocess.CompletedProcess(argv, 1, "", fail)
        if cmd == "display-message":
            out = f"{self.own}\n"
        elif cmd == "has-session":
            rc = 0 if argv[3][1:] in self.sessions else 1
        elif cmd == "kill-session":
            rc = 0 if self.sessions.pop(argv[3][1:], None) is not None else 1
        elif cmd == "list-sessions":
            out = "".join("\t".join((name, *o)) + "\n" for name, o in self.sessions.items()) + self.extra
        elif cmd == "list-clients":
            out = self.clients.get(argv[3][1:], "")
        elif cmd == "list-panes":
            out = self.panes
        return subprocess.CompletedProcess(argv, rc, out, "")

    def ran(self, cmd):
        return [c for c in self.calls if c[1] == cmd]


def attached(*argv, own=None):
    """Runs `workers.py attach argv…` on a fake tmux server (Server), the caller in tmux session `own` (None: outside
    tmux); fails unless it exits 0. What start and next-event need first."""
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    if own:
        env.update(TMUX="/tmp/tmux-1/default,1,0", TMUX_PANE="%3")
    err = io.StringIO()
    with mock.patch.object(workers.subprocess, "run", Server(own)), mock.patch.dict(os.environ, env, clear=True), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = workers.main(["attach", *argv])
    if rc:
        raise AssertionError(f"attach exited {rc}: {err.getvalue()}")


MGR = {"session": "mgr", "since": WHEN}
OTHER = {"session": "other", "since": WHEN}


class RosterCase(WorkerCase):
    """mgr's manager directory, its roster seeded by a test, on a fake tmux server (Server)."""

    def setUp(self):
        super().setUp()
        self.agent_pm = hermetic.home(self)
        self.mdir = os.path.join(self.agent_pm, "managers", "mgr")
        self.path = os.path.join(self.mdir, "roster.json")
        self.lock = os.path.join(self.mdir, "roster.lock")

    def seed(self, holder=None, cursor=0, gen=0, **entries):
        """roster.json holding these entries; returns its bytes."""
        manager.ensure(self.mdir)
        with open(self.path, "w") as f:
            json.dump({"version": 1, "holder": holder, "cursor": cursor, "gen": gen, "entries": entries}, f)
        return self.bytes()

    def bytes(self):
        with open(self.path, "rb") as f:
            return f.read()

    def entries(self):
        return json.loads(self.bytes())["entries"]

    def held(self):
        """The FR-13 refusal for OTHER, as main prints it."""
        return (f"workers: manager directory {self.mdir}: held by tmux session other since {WHEN}; ask that manager to "
                "run workers.py release, or end that session\n")

    def not_attached(self):
        return f"workers: manager directory {self.mdir} not attached: run workers.py attach first\n"

    def run_main(self, server, *argv):
        """main's (exit code, stdout, stderr) for argv, the caller in tmux session server.own."""
        env = dict(self.env)
        if server.own:
            env.update(TMUX="/tmp/tmux-1/default,1,0", TMUX_PANE="%3")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(workers.subprocess, "run", server), mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = workers.main(list(argv))
        return rc, out.getvalue(), err.getvalue()


class AttachTest(RosterCase):
    """workers.py attach, the caller in tmux session mgr unless a test says otherwise; show is self.show."""

    def setUp(self):
        super().setUp()
        self.shows, self.then = [], None
        p = mock.patch.object(tui_claude, "show", self.show)
        p.start()
        self.addCleanup(p.stop)

    def show(self, session, **kw):
        """tui_claude.show as a recorder: (session, its keywords, whether roster.lock was free) in self.shows; it
        records @pane %9 and @opener mgr on the session as tui_claude's does, then runs self.then."""
        self.shows.append((session, kw, lock_free(self.lock)))
        sessions = kw["proc"].sessions
        sessions[session] = (*sessions[session][:2], "%9", "mgr")
        if self.then:
            self.then()

    def config(self, text):
        with open(os.path.join(self.agent_pm, "core.local.toml"), "w") as f:
            f.write(text)

    def attach(self, server, *argv):
        """main's (exit code, stdout, stderr) for `attach *argv`."""
        return self.run_main(server, "attach", *argv)

    def synced(self, server, *argv):
        """attach's stdout; asserts exit 0 and nothing on stderr."""
        rc, out, err = self.attach(server, *argv)
        self.assertEqual((rc, err), (0, ""))
        return out

    def test_first_attach_makes_the_directory_and_files_and_takes_the_lease(self):
        with mock.patch.object(manager, "now", return_value=WHEN):
            self.assertEqual(self.synced(Server()), HEADER + tail(self.mdir))
        self.assertEqual(stat.S_IMODE(os.stat(self.mdir).st_mode), 0o700)
        for name in ("events", "roster.json", "roster.lock"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.mdir, name)).st_mode), 0o600, name)
        holder = {"session": "mgr", "since": WHEN}
        self.assertEqual(json.loads(self.bytes()),
                         {"version": 1, "holder": holder, "cursor": 0, "gen": 0, "entries": {}})
        with mock.patch.object(manager, "now", return_value="2026-10-10T00:00:00+00:00"):
            self.synced(Server())
        self.assertEqual(json.loads(self.bytes())["holder"], holder)

    def test_manager_option_names_the_directory_the_own_session_holds(self):
        self.synced(Server("m2"), "--manager", "mgr")
        self.assertEqual(json.loads(self.bytes())["holder"]["session"], "m2")

    def test_refused_by_a_live_other_holder_writing_nothing(self):
        for own in ("mgr", None):
            with self.subTest(own=own):
                before = self.seed(holder=OTHER, w1=entry())
                server = Server(own, sessions={"other": opts()})
                self.assertEqual(self.attach(server, "--manager", "mgr"), (1, "", self.held()))
                self.assertEqual(self.bytes(), before)
                self.assertEqual(server.ran("list-sessions"), [])

    def test_outside_tmux_prints_the_notice_and_leaves_the_holder(self):
        notice = f"workers: not in tmux: no lease on {self.mdir}; another manager may attach\n"
        for holder in (None, {"session": "old", "since": WHEN}):
            with self.subTest(holder=holder):
                self.seed(holder=holder)
                self.assertEqual(self.attach(Server(None), "--manager", "mgr"), (0, HEADER + tail(self.mdir), notice))
                self.assertEqual(json.loads(self.bytes())["holder"], holder)

    def test_outside_tmux_without_manager_exits_1_and_makes_nothing(self):
        self.assertEqual(self.attach(Server(None)),
                         (1, "", "workers: no manager directory: run inside tmux or give --manager <name>\n"))
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_a_bad_core_config_exits_1_before_anything_is_made(self):
        self.config("workers_per_column = 0\n")
        self.assertEqual(self.attach(Server()),
                         (1, "", "workers: core config: workers_per_column: want an integer from 1 to 9999\n"))
        self.assertFalse(os.path.exists(os.path.join(self.agent_pm, "managers")))

    def test_a_worker_renamed_in_tmux_is_rekeyed_by_its_sid(self):
        old = entry(resume=[PY, SCRIPT["worker"], "start", "w1"], state="gone")
        self.seed(w1=old)
        self.synced(Server(sessions={"w9": opts(SID, "done", "%4", "mgr")}, clients={"w9": SHOWN}))
        self.assertEqual(self.entries(), {"w9": {**old, "state": "done", "pane": "%4", "opener": "mgr"}})

    def test_no_rename_without_exactly_one_match_free_to_take(self):
        cases = {"two sessions have its sid": ({"w8": opts(SID), "w9": opts(SID)}, {}),
                 "the match is an entry": ({"w9": opts(SID)}, {"w9": entry("role", sid=SID2)}),
                 "the match is not a NAME": ({"a.b": opts(SID)}, {}),
                 "no session has its sid": ({"w9": opts(SID2)}, {})}
        for why, (sessions, others) in cases.items():
            with self.subTest(why):
                self.seed(w1=entry(), **others)
                self.synced(Server(sessions=sessions, clients=dict.fromkeys(sessions, SHOWN)))
                self.assertEqual(set(self.entries()), {"w1", *others})
                self.assertEqual(self.entries()["w1"]["state"], "gone")

    def test_only_a_worker_with_a_sid_is_renamed(self):
        self.seed(r1=entry("role", cwd=self.dir), p1=entry("pipeline", sid=SID2, cwd=self.dir), w1=entry(sid=None))
        self.synced(Server(sessions={"x1": opts(SID), "x2": opts(SID2), "x3": opts()},
                           clients=dict.fromkeys(("x1", "x2", "x3"), SHOWN)))
        self.assertEqual({n: e["state"] for n, e in self.entries().items()}, {"r1": "gone", "p1": "gone", "w1": "gone"})

    def test_a_short_row_is_not_live(self):
        self.seed(w1=entry())
        self.synced(Server(extra=f"w1\t{SID}\n"))
        self.assertEqual(self.entries()["w1"]["state"], "gone")

    def test_a_live_workers_state_is_its_sessions_when_known_else_working(self):
        states = {"working": "working", "done": "done", "blocked": "blocked", "dead": "dead", "": "working",
                  "gone": "working", "finished": "working", "idle": "working", "Done": "working"}
        names = {f"w{i}": pair for i, pair in enumerate(states.items())}
        self.seed(**{name: entry(state="gone") for name in names})
        self.synced(Server(sessions={name: opts(SID, have) for name, (have, _) in names.items()},
                           clients=dict.fromkeys(names, SHOWN)))
        self.assertEqual({n: e["state"] for n, e in self.entries().items()},
                         {n: want for n, (_, want) in names.items()})

    def test_a_worker_without_a_session_is_gone(self):
        self.seed(w1=entry(state="blocked", pane="%4"))
        self.assertEqual(self.synced(Server()), HEADER + f"w1\tworker\t{SID}\tgone\t-\t/w\t%4\n"
                                                         f"resume w1: {PY} /x/workers.py --go\n" + tail(self.mdir))
        self.assertEqual(self.entries()["w1"]["state"], "gone")
        self.assertEqual(self.shows, [])

    def test_a_worker_without_a_session_is_gone_whatever_run_record_its_cwd_holds(self):
        entries = {}
        for status in ("needs_input", "done", "failed"):
            cwd = os.path.join(self.dir, status)
            os.mkdir(cwd)
            with open(os.path.join(cwd, "run.jsonl"), "w") as f:
                f.write(result(outcome={"status": status}))
            entries[status] = entry(cwd=cwd)
        self.seed(**entries)
        out = self.synced(Server())
        self.assertEqual({n: e["state"] for n, e in self.entries().items()}, dict.fromkeys(entries, "gone"))
        self.assertEqual([line.split(":")[0] for line in out.splitlines() if line.startswith("resume ")],
                         [f"resume {name}" for name in sorted(entries)])

    def test_a_role_or_pipeline_is_live_by_its_driver_or_tui_session(self):
        """Driver only: working, placement kept; tui: its state and placement, the driver live or not."""
        for kind in ("role", "pipeline"):
            with self.subTest(kind=kind):
                self.seed(d1=entry(kind, cwd=self.dir, state="gone", tui="t1", pane="%1"),
                          d2=entry(kind, cwd=self.dir, state="done"),
                          t3=entry(kind, cwd=self.dir, state="gone", tui="t3t"),
                          b4=entry(kind, cwd=self.dir, state="gone", tui="t4"))
                server = Server(sessions={"d1": opts(state="dead", pane="%7", opener="x"), "d2": opts(state="blocked"),
                                          "t3t": opts(SID, "blocked", "%8", "mgr"), "b4": opts(state="dead"),
                                          "t4": opts(state="done", pane="%6")},
                                clients={"t3t": SHOWN, "t4": SHOWN})
                self.synced(server)
                self.assertEqual({n: (e["state"], e["pane"], e["opener"]) for n, e in self.entries().items()},
                                 {"d1": ("working", "%1", None), "d2": ("working", None, None),
                                  "t3": ("blocked", "%8", "mgr"), "b4": ("done", "%6", None)})
                self.assertEqual([c[3] for c in server.ran("list-clients")], ["=t4", "=t3t"])

    def test_a_role_or_pipeline_without_a_session_is_finished_waiting_or_gone_by_its_run_record(self):
        done, failed, tail = result(outcome={"status": "done"}), result(outcome={"status": "failed"}), workers.RUN_TAIL
        asking = result(outcome={"status": "needs_input"})
        pad = ("x" * 99 + "\n") * (tail // 100 + 1)
        cut = tail - len(done)
        records = {
            "done": (done, "finished"), "failed": (failed, "finished"),
            "later-lines-skipped": (done + '{"kind": "end"}\nnot json\n' + "[" * 200000 + '\n{"kind": "res',
                                    "finished"),
            "last-result-wins": (done + result(error="stopped: KeyboardInterrupt"), "gone"),
            "failed-after-an-error": (result(error="boom") + failed, "finished"),
            "needs-input": (asking, "waiting"),
            "answered": (asking + done, "finished"),
            "asking-after-done": (done + asking, "waiting"),
            "needs-input-after-an-error": (result(error="boom") + asking, "waiting"),
            "error-after-needs-input": (asking + result(error="stopped: KeyboardInterrupt"), "gone"),
            "error": (result(error="boom"), "gone"),
            "malformed": ("{\nnot json\n\udcff\n", "gone"),
            "empty": ("", "gone"),
            "outcome-a-string": (result(outcome="done"), "gone"),
            "outcome-a-list": (result(outcome=[{"status": "done"}]), "gone"),
            "status-a-list": (result(outcome={"status": ["done"]}), "gone"),
            "status-an-object": (result(outcome={"status": {"needs_input": 1}}), "gone"),
            "status-other": (result(outcome={"status": "waiting"}), "gone"),
            "no-status": (result(outcome={}), "gone"),
            "kind-outcome": (json.dumps({"kind": "outcome", "outcome": {"status": "done"}}) + "\n", "gone"),
            "a-list-line": (json.dumps([{"kind": "result", "outcome": {"status": "done"}}]) + "\n", "gone"),
            "beyond-the-tail": (done + pad, "gone"),
            "within-the-tail": (pad + done, "finished"),
            "cut-by-the-tail": ("x" + done + "p\n" * (cut // 2) + "p" * (cut % 2), "gone"),
            "starts-the-tail": ("x\n" + done + "p\n" * (cut // 2) + "p" * (cut % 2), "finished"),
        }
        entries, want = {}, {}
        for i, (name, (text, state)) in enumerate(records.items()):
            cwd = os.path.join(self.dir, name)
            os.mkdir(cwd)
            with open(os.path.join(cwd, "run.jsonl"), "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(text)
            entries[name], want[name] = entry(("role", "pipeline")[i % 2], cwd=cwd), state
        done_cwd = os.path.join(self.dir, "done", "run.jsonl")
        for name, make in (("missing", lambda path: None), ("symlink", lambda path: os.symlink(done_cwd, path)),
                           ("fifo", os.mkfifo), ("directory", os.mkdir)):
            cwd = os.path.join(self.dir, name)
            os.mkdir(cwd)
            make(os.path.join(cwd, "run.jsonl"))
            entries[name], want[name] = entry("role", cwd=cwd), "gone"
        self.seed(**entries)
        with within():
            self.synced(Server())
        self.assertEqual({n: e["state"] for n, e in self.entries().items()}, want)

    def test_a_live_sessions_pane_and_opener_are_synced_when_valid(self):
        cases = {"w1": ("%7", "mgr", "%7", "mgr"),
                 "w2": ("5E1B-C0FFEE", "w0t0p0:5E1B-C0FFEE", "5E1B-C0FFEE", "w0t0p0:5E1B-C0FFEE"),
                 "w3": ("", "", "%1", "old"), "w4": ("%x", "a b", "%1", "old"), "w5": ("%8", "a.b", "%8", "old"),
                 "w6": ("a b", "new", "%1", "new")}
        self.seed(**{name: entry(pane="%1", opener="old") for name in cases})
        self.synced(Server(sessions={n: opts(SID, "working", p, o) for n, (p, o, _, _) in cases.items()},
                           clients=dict.fromkeys(cases, SHOWN)))
        self.assertEqual({n: (e["pane"], e["opener"]) for n, e in self.entries().items()},
                         {n: (p, o) for n, (_, _, p, o) in cases.items()})

    def test_a_live_session_no_client_shows_is_reopened_with_the_lock_free(self):
        self.config("workers_per_column = 2\n")
        self.seed(w1=entry(split="below", split_from="mgr"), r1=entry("role", tui="r1-tui", split="right"),
                  w2=entry(), w3=entry(sid=SID2), r2=entry("role", sid=SID2, cwd=self.dir, tui="r2-tui"))
        server = Server(sessions={"w1": opts(SID), "r1-tui": opts(state="done"), "w2": opts(SID), "r2": opts()},
                        clients={"w2": SHOWN})
        with mock.patch.object(manager, "roster", wraps=manager.roster) as roster:
            out = self.synced(server)
        self.assertEqual(self.shows, [
            ("r1-tui", {"split": "right", "split_from": None, "per_column": 2, "proc": server}, True),
            ("w1", {"split": "below", "split_from": "mgr", "per_column": 2, "proc": server}, True)])
        self.assertEqual(roster.call_count, 2)
        self.assertEqual({n: (e["pane"], e["opener"]) for n, e in self.entries().items()},
                         {"w1": ("%9", "mgr"), "r1": ("%9", "mgr"), "w2": (None, None), "w3": (None, None),
                          "r2": (None, None)})
        self.assertIn(f"w1\tworker\t{SID}\tworking\t-\t/w\t%9\n", out)

    def test_no_reopen_no_second_roster_block(self):
        self.seed(w1=entry())
        with mock.patch.object(manager, "roster", wraps=manager.roster) as roster:
            self.synced(Server(sessions={"w1": opts(SID)}, clients={"w1": SHOWN}))
        self.assertEqual((roster.call_count, self.shows), (1, []))

    def test_the_default_per_column(self):
        self.seed(w1=entry())
        self.synced(Server(sessions={"w1": opts(SID)}))
        self.assertEqual([kw["per_column"] for _, kw, _ in self.shows], [None])

    def test_placement_is_recorded_only_for_an_entry_still_the_same(self):
        def drop():
            with manager.roster(self.mdir) as r:
                manager.remove(r, "w1")
        cases = {"same": (None, {**entry(), "pane": "%9", "opener": "mgr"}),
                 "sid changed": (lambda: manager.put(self.mdir, "w1", entry(sid=SID2)), entry(sid=SID2)),
                 "kind changed": (lambda: manager.put(self.mdir, "w1", entry("pipeline")), entry("pipeline")),
                 "removed": (drop, None)}
        for why, (then, want) in cases.items():
            with self.subTest(why):
                self.seed(w1=entry())
                self.then = then
                self.synced(Server(sessions={"w1": opts(SID)}))
                self.assertEqual(self.entries().get("w1"), want)

    def test_a_shown_session_is_marked_by_its_most_recently_active_client(self):
        panes = "/dev/ttys001\tmgr\n/dev/ttys002\tother\n/dev/ttys003\ta.b\n\n"
        cases = {"w1": ("100 /dev/ttys002\n300 /dev/ttys001\n200 /dev/ttys003\n", "mgr"),
                 "w2": ("20 /dev/ttys001\n100 /dev/ttys002\n", "other"),
                 "w3": ("300 /dev/ttys003\n100 /dev/ttys001\n", "a terminal"),
                 "w4": ("9 /dev/ttys009\n", "a terminal")}
        self.seed(**{name: entry() for name in cases})
        server = Server(sessions={name: opts(SID) for name in cases}, clients={n: c for n, (c, _) in cases.items()},
                        panes=panes)
        self.assertEqual(self.synced(server), HEADER + "".join(
            f"{n}\tworker\t{SID}\tworking\t-\t/w\t-\tshown in {where}\n" for n, (_, where) in cases.items())
                         + tail(self.mdir))
        self.assertEqual(server.ran("list-clients")[0], ["tmux", "list-clients", "-t", "=w1", "-F",
                                                         "#{client_activity} #{client_tty}"])
        self.assertEqual(server.ran("list-panes"), [["tmux", "list-panes", "-a", "-F", "#{pane_tty}\t#{session_name}"]])
        self.assertEqual(self.shows, [])

    def test_table_and_recovery_commands_none_for_a_waiting_run(self):
        cwd = os.path.join(self.dir, "it's a role")
        finished, asking = os.path.join(self.dir, "finished"), os.path.join(self.dir, "asking")
        for d, status in ((cwd, None), (finished, "done"), (asking, "needs_input")):
            os.mkdir(d)
            if status:
                with open(os.path.join(d, "run.jsonl"), "w") as f:
                    f.write(result(outcome={"status": status}))
        self.seed(b=entry(note="check the logs", pane="%4"),
                  a=entry(sid=None, resume=[PY, "/x/workers.py", "; rm -rf ~"]),
                  B=entry("role", cwd=cwd, resume=[PY, "/x/drive.py", "--role", "pm"]),
                  C=entry("role", sid=None, cwd=cwd),
                  p1=entry("pipeline", cwd=cwd, resume=[PY, "/x/router.py", "--plan", "/p q"]),
                  f1=entry("pipeline", cwd=finished), n1=entry("role", cwd=asking), n2=entry("pipeline", cwd=asking))
        out = self.synced(Server(sessions={"b": opts(SID, "working", "%4")}, clients={"b": SHOWN},
                                 panes="/dev/ttys001\tmgr\n"))
        role = (f"cd {shlex.quote(cwd)} && {PY} /x/drive.py --role pm --sid {SID} --resume "
                "--input 'Continue the unfinished task.'")
        self.assertEqual(out, HEADER + nl(f"B\trole\t{SID}\tgone\t-\t{cwd}\t-",
                                          f"C\trole\t-\tgone\t-\t{cwd}\t-",
                                          "a\tworker\t-\tgone\t-\t/w\t-",
                                          f"b\tworker\t{SID}\tworking\tcheck the logs\t/w\t%4\tshown in mgr",
                                          f"f1\tpipeline\t{SID}\tfinished\t-\t{finished}\t-",
                                          f"n1\trole\t{SID}\twaiting\t-\t{asking}\t-",
                                          f"n2\tpipeline\t{SID}\twaiting\t-\t{asking}\t-",
                                          f"p1\tpipeline\t{SID}\tgone\t-\t{cwd}\t-",
                                          f"resume B: {role}",
                                          "resume C: none (no sid)",
                                          f"resume a: {PY} /x/workers.py '; rm -rf ~'",
                                          f"resume p1: {PY} /x/router.py --plan '/p q'") + tail(self.mdir))
        self.assertEqual(shlex.split(role)[:4], ["cd", cwd, "&&", PY])
        self.assertEqual(shlex.split(out.splitlines()[-6].split(": ", 1)[1]), [PY, "/x/workers.py", "; rm -rf ~"])

    def test_an_invalid_entry_is_skipped_with_one_stderr_line(self):
        self.seed(w1=entry(), bad=entry(sid="; rm -rf ~"), rel=entry(resume=["python3", "/x/workers.py"]))
        rc, out, err = self.attach(Server(sessions={"w1": opts(SID)}, clients={"w1": SHOWN}))
        self.assertEqual((rc, out), (0, HEADER + f"w1\tworker\t{SID}\tworking\t-\t/w\t-\tshown in a terminal\n"
                                     + tail(self.mdir)))
        self.assertEqual(err, "manager: roster.json: entry 'bad': sid: '; rm -rf ~': want a session id or null\n"
                              "manager: roster.json: entry 'rel': resume[0]: 'python3': want an absolute path\n")
        self.assertEqual(set(self.entries()), {"w1"})

    def test_no_tmux_server_means_no_live_session(self):
        for err in ("no server running on /tmp/tmux-501/default\n",
                    "error connecting to /tmp/tmux-501/default (No such file or directory)\n"):
            with self.subTest(err=err):
                self.seed(w1=entry())
                rc, out, _ = self.attach(Server(None, fail={"list-sessions": err}), "--manager", "mgr")
                self.assertEqual((rc, out.splitlines()[1]), (0, f"w1\tworker\t{SID}\tgone\t-\t/w\t-"))
                self.assertEqual(self.entries()["w1"]["state"], "gone")

    def test_any_other_list_sessions_failure_writes_nothing(self):
        for fail, msg in (("boom\n", "tmux list-sessions: boom"),
                          (OSError(errno.ENOENT, "No such file or directory"), "tmux: No such file or directory")):
            with self.subTest(msg):
                before = self.seed(w1=entry())
                self.assertEqual(self.attach(Server(fail={"list-sessions": fail})), (1, "", f"workers: {msg}\n"))
                self.assertEqual(self.bytes(), before)
        os.unlink(self.path)
        self.assertEqual(self.attach(Server(fail={"list-sessions": "boom\n"}))[0], 1)
        self.assertFalse(os.path.exists(self.path))

    def test_a_failed_list_clients_skips_that_entry(self):
        """No reopen and no `shown in` for it; the rest as usual, the second block included."""
        for fail in ("can't find session: w1\n", OSError(errno.ENOENT, "No such file or directory")):
            with self.subTest(fail=fail):
                self.shows = []
                self.seed(w1=entry(), w2=entry(sid=SID2), w3=entry(sid=None))
                server = Server(sessions={"w1": opts(), "w2": opts()}, fail={"list-clients -t =w1": fail})
                self.assertEqual(self.synced(server), HEADER + nl(f"w1\tworker\t{SID}\tworking\t-\t/w\t-",
                                                                  f"w2\tworker\t{SID2}\tworking\t-\t/w\t%9",
                                                                  "w3\tworker\t-\tgone\t-\t/w\t-",
                                                                  f"resume w3: {PY} /x/workers.py --go")
                                 + tail(self.mdir))
                self.assertEqual([s for s, _, _ in self.shows], ["w2"])
                self.assertEqual({n: (e["pane"], e["opener"]) for n, e in self.entries().items()},
                                 {"w1": (None, None), "w2": ("%9", "mgr"), "w3": (None, None)})

    def test_a_failed_list_panes_is_shown_in_a_terminal(self):
        for fail in ("boom\n", OSError(errno.ENOENT, "No such file or directory")):
            with self.subTest(fail=fail):
                self.seed(w1=entry())
                server = Server(sessions={"w1": opts(SID)}, clients={"w1": SHOWN}, fail={"list-panes": fail})
                self.assertEqual(self.synced(server),
                                 HEADER + f"w1\tworker\t{SID}\tworking\t-\t/w\t-\tshown in a terminal\n"
                                 + tail(self.mdir))

    def test_a_reopen_outside_tmux_prints_shows_messages_on_stderr(self):
        executable(os.path.join(self.bin, "tmux"), "#!/bin/sh\n")
        self.seed(w1=entry())
        with mock.patch.object(tui_claude, "show", SHOW):
            rc, out, err = self.attach(Server(None, sessions={"w1": opts(SID)}), "--manager", "mgr")
        cmd = tui_claude.attach_command("w1")
        self.assertEqual((rc, out), (0, HEADER + f"w1\tworker\t{SID}\tworking\t-\t/w\t-\n" + tail(self.mdir)))
        self.assertEqual(err, f"workers: not in tmux: no lease on {self.mdir}; another manager may attach\n"
                              f"tui: session w1: {cmd}\n"
                              f"tui: show: {tui_claude.NO_PANE.format('not in tmux')}; watch it with {cmd}\n")


class LeaseTest(RosterCase):
    """start and next-event check the lease (manager.check) before any tmux call and make nothing but start's entry
    (StartEntryTest) and next-event's cursor; start's session (spawned) and next_event are fakes."""
    CMDS = (["start", "w1"], ["next-event", "--after", "0", "--gen", "0"])
    FRESH = {"version": 1, "holder": None, "cursor": 0, "gen": 0, "entries": {}}

    def setUp(self):
        super().setUp()
        p = mock.patch.object(workers, "next_event", return_value=(1, "10:00:01 w1 outcome done"))
        self.fakes = [self.enterContext(spawned()), p.start()]
        self.addCleanup(p.stop)

    def rest(self, cmd):
        """roster.json without start's entry; None when missing."""
        if not os.path.exists(self.path):
            return None
        r = json.loads(self.bytes())
        if cmd[0] == "start":
            del r["entries"]["w1"]
        return r

    def ran(self, server, cmd):
        """(exit code, stderr, whether start or next_event ran) for `cmd --manager mgr`."""
        for fake in self.fakes:
            fake.reset_mock()
        rc, _, err = self.run_main(server, *cmd, "--manager", "mgr")
        return rc, err, any(fake.called for fake in self.fakes)

    def test_refused_while_another_live_session_holds(self):
        before = self.seed(OTHER)
        for own in ("mgr", None):
            for cmd in self.CMDS:
                with self.subTest(own=own, cmd=cmd[0]):
                    self.assertEqual(self.ran(Server(own, sessions={"other": opts()}), cmd), (1, self.held(), False))
                    self.assertEqual(self.bytes(), before)

    def test_refused_in_tmux_when_not_the_holder(self):
        for holder in (None, OTHER):
            before = self.seed(holder)
            for cmd in self.CMDS:
                with self.subTest(holder=holder, cmd=cmd[0]):
                    self.assertEqual(self.ran(Server(), cmd), (1, self.not_attached(), False))
                    self.assertEqual(self.bytes(), before)

    def test_refused_when_the_directory_is_missing_which_stays_missing(self):
        for own in ("mgr", None):
            for cmd in self.CMDS:
                with self.subTest(own=own, cmd=cmd[0]):
                    self.assertEqual(self.ran(Server(own), cmd), (1, self.not_attached(), False))
                    self.assertEqual(os.listdir(self.agent_pm), [])

    def test_allowed_for_the_holder(self):
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                before = json.loads(self.seed(MGR))
                self.assertEqual(self.ran(Server(), cmd), (0, "", True))
                self.assertEqual(self.rest(cmd), before)

    def test_allowed_outside_tmux_without_a_live_holder(self):
        """start writes only its entry; next-event only the cursor it records; the holder left null."""
        manager.events(self.mdir)   # as drive.py and router.py --tui still make it
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0]):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
                self.assertEqual(self.ran(Server(None), cmd), (0, "", True))
                self.assertEqual(self.rest(cmd), self.FRESH)
        for cmd in self.CMDS:
            with self.subTest(cmd=cmd[0], holder="dead"):
                before = json.loads(self.seed(OTHER))
                self.assertEqual(self.ran(Server(None), cmd), (0, "", True))
                self.assertEqual(self.rest(cmd), before)


class StartEntryTest(RosterCase):
    """start's roster entry (manager.record) in mgr's directory, which mgr holds; tmux is a Fake (has-session: every
    session live)."""

    def setUp(self):
        super().setUp()
        self.seed(MGR)

    def start(self, fake=None, **kw):
        return workers.start("w1", self.mdir, own="mgr", cwd=self.dir, env=self.env, proc=fake or Fake(), **kw)

    def resume(self, sid, *rest):
        """w1's recovery argv for sid, rest after its --cwd."""
        return [sys.executable, WORKERS, "start", "w1", "--manager", "mgr", "--resume", sid, "--cwd", self.dir, *rest]

    def lines(self, err):
        """err's lines but show's (tui: …)."""
        return [x for x in err.getvalue().splitlines() if not x.startswith("tui: ")]

    def test_the_worker_entry(self):
        cases = (({}, (), []),
                 ({}, ("--permission-mode", "auto"), ["--", "--permission-mode", "auto"]),
                 ({}, ("--model=m", "--", "-c"), ["--", "--model=m", "--", "-c"]),
                 ({"split": "below"}, (), ["--split", "below"]),
                 ({"split_from": "x"}, ("--model", "m"), ["--split-from", "x", "--", "--model", "m"]),
                 ({"split": "right", "split_from": "x"}, (), ["--split", "right", "--split-from", "x"]))
        for kw, flags, rest in cases:
            with self.subTest(**kw, flags=flags):
                with mock.patch.object(manager, "now", return_value=WHEN):
                    sid = self.start(flags=flags, note="check it", **kw)
                self.assertEqual(self.entries(), {"w1": entry(sid=sid, cwd=self.dir, resume=self.resume(sid, *rest),
                                                              note="check it", **kw)})

    def test_opener_and_pane_from_the_session_options_each_only_when_valid(self):
        cases = {"tmux": ("%5", "mgr", "%5", "mgr"),
                 "iTerm2": ("5E1B-C0FFEE", "w0t0p0:5E1B-C0FFEE", "5E1B-C0FFEE", "w0t0p0:5E1B-C0FFEE"),
                 "invalid pane": ("%x", "mgr", None, "mgr"), "invalid opener": ("%5", "a.b", "%5", None),
                 "unset": ("", "", None, None)}
        for why, (pane, opener, *want) in cases.items():
            with self.subTest(why):
                rows = f"w2\t\t\t%1\tw2\nw1\t{SID}\t\t{pane}\t{opener}\n"
                self.start(Fake(results={"list-sessions": lambda argv, rows=rows: rows}))
                e = self.entries()["w1"]
                self.assertEqual([e["pane"], e["opener"]], want)

    def test_no_row_or_a_failing_list_sessions_places_nothing(self):
        failing = Fake()
        failing.oserror = lambda argv: argv[1] == "list-sessions"
        for why, fake in (("no row", Fake(results={"list-sessions": lambda argv: "w2\t\t\t%1\tmgr\n"})),
                          ("failing", failing)):
            with self.subTest(why):
                self.start(fake)
                e = self.entries()["w1"]
                self.assertEqual((e["pane"], e["opener"]), (None, None))

    def test_a_claude_that_exits_at_once_leaves_a_working_entry_attach_syncs(self):
        fake = Fake()
        fake.spawned, fake.text = DEAD_PANE, DEAD_TEXT
        with self.assertRaisesRegex(workers.WorkersError, "^w1: claude exited 1 at once"):
            self.start(fake)
        sid = fake.store["@sid"]
        self.assertEqual((self.entries()["w1"]["sid"], self.entries()["w1"]["state"]), (sid, "working"))
        rc, _, err = self.run_main(Server(sessions={"w1": opts(sid, "dead")}, clients={"w1": SHOWN}), "attach")
        self.assertEqual((rc, err, self.entries()["w1"]["state"]), (0, "", "dead"))

    def test_a_re_start_replaces_the_entry_keeping_its_note_only_for_the_same_sid(self):
        first = self.start(note="keep")
        self.assertEqual(self.start(resume=first), first)
        self.assertEqual(self.entries()["w1"]["note"], "keep")
        self.start(resume=first, note="new")
        self.assertEqual(self.entries()["w1"]["note"], "new")
        second = self.start()
        self.assertNotEqual(second, first)
        self.assertEqual({n: (e["sid"], e["note"]) for n, e in self.entries().items()}, {"w1": (second, None)})

    def test_an_empty_note_is_none(self):
        self.start(note="")
        self.assertIsNone(self.entries()["w1"]["note"])

    def test_the_roster_lock_is_free_during_the_early_watch(self):
        free = []
        self.sleep.side_effect = lambda secs: free.append(lock_free(self.lock))
        self.start()
        self.assertEqual(free, [True] * 10)

    def test_another_live_holder_starts_nothing(self):
        before = self.seed(OTHER)
        fake = Fake()
        with self.assertRaises(workers.WorkersError) as cm:
            self.start(fake)
        self.assertEqual(f"workers: {cm.exception}\n", self.held())
        self.assertEqual((fake.new_sessions(), self.bytes()), ([], before))

    def test_a_take_over_before_the_write_writes_nothing_the_worker_runs(self):
        def take_over(argv):
            with manager.roster(self.mdir) as r:
                r["holder"] = OTHER
            return ""
        fake = Fake(results={"list-sessions": take_over})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.start(fake), fake.store["@sid"])
        held = self.held().removeprefix("workers: ").removesuffix("\n")
        self.assertEqual(self.lines(err), [f"manager: entry w1 not written: {held}"])
        self.assertEqual(self.entries(), {})
        self.assertNotIn("kill-session", [c[1] for c in fake.calls])

    def test_a_session_that_fails_to_start_writes_no_entry(self):
        option = Fake()
        option.fail_set = "@flags"
        for why, fake in (("new-session", Fake(new_err="boom\n")), ("option", option)):
            with self.subTest(why):
                with self.assertRaises(workers.WorkersError):
                    self.start(fake)
                self.assertEqual(self.entries(), {})

    def test_attach_prints_the_recovery_once_the_session_is_gone(self):
        sid = self.start(flags=("--permission-mode", "auto"))
        resume = self.resume(sid, "--", "--permission-mode", "auto")
        rc, out, err = self.run_main(Server(), "attach")
        self.assertEqual((rc, err), (0, ""))
        self.assertTrue(out.endswith(f"resume w1: {shlex.join(resume)}\n{tail(self.mdir)}"), out)


class RosterCommandsTest(RosterCase):
    """release, note, forget and stop, the caller in tmux session mgr unless a test says otherwise."""
    CMDS = (["release"], ["note", "w1", "x"], ["forget", "w1"], ["stop", "w1"])

    def test_refused_without_the_lease_writing_nothing(self):
        cases = {"another live holder": (OTHER, {"other": opts()}, self.held()),
                 "no holder": (None, {}, self.not_attached()),
                 "a dead other holder": (OTHER, {}, self.not_attached())}
        for why, (holder, sessions, msg) in cases.items():
            for cmd in self.CMDS:
                with self.subTest(why, cmd=cmd[0]):
                    before = self.seed(holder, w1=entry())
                    server = Server(sessions={**sessions, "w1": opts(SID)})
                    self.assertEqual(self.run_main(server, *cmd), (1, "", msg))
                    self.assertEqual(server.ran("kill-session"), [])
                    self.assertEqual(self.bytes(), before)

    def test_refused_when_the_directory_is_missing_which_stays_missing(self):
        for own in ("mgr", None):
            for cmd in self.CMDS:
                with self.subTest(own=own, cmd=cmd[0]):
                    self.assertEqual(self.run_main(Server(own), *cmd, "--manager", "mgr"), (1, "", self.not_attached()))
                    self.assertEqual(os.listdir(self.agent_pm), [])

    def test_release_by_the_holder_keeps_the_cursor(self):
        self.seed(MGR, cursor=7, w1=entry())
        self.assertEqual(self.run_main(Server(), "release"), (0, f"workers: released {self.mdir}\n", ""))
        self.assertEqual(json.loads(self.bytes()),
                         {"version": 1, "holder": None, "cursor": 7, "gen": 0, "entries": {"w1": entry()}})

    def test_release_outside_tmux_without_a_live_holder(self):
        self.seed(OTHER, cursor=7)
        self.assertEqual(self.run_main(Server(None), "release", "--manager", "mgr"),
                         (0, f"workers: released {self.mdir}\n", ""))
        r = json.loads(self.bytes())
        self.assertEqual((r["holder"], r["cursor"]), (None, 7))

    def test_note_sets_and_an_empty_one_clears(self):
        self.seed(MGR, w1=entry(), w2=entry(note="old"))
        for argv in (["w1", "check the logs"], ["w2", ""]):
            self.assertEqual(self.run_main(Server(), "note", *argv), (0, "", ""))
        self.assertEqual(self.entries(), {"w1": entry(note="check the logs"), "w2": entry()})

    def test_note_of_500_characters(self):
        self.seed(MGR, w1=entry())
        self.assertEqual(self.run_main(Server(), "note", "w1", "x" * 500), (0, "", ""))
        self.assertEqual(self.entries()["w1"]["note"], "x" * 500)

    def test_a_long_or_unprintable_note_is_refused_writing_nothing(self):
        cases = {"x" * 501: "note: 501 characters: want at most 500",
                 **{text: f"note: {text!r}: {UNPRINTABLE}" for text in ("a\nb", "a\x9fb", "a b", "\x7f", "a\tb")}}
        for text, msg in cases.items():
            with self.subTest(msg):
                before = self.seed(MGR, w1=entry())
                self.assertEqual(self.run_main(Server(), "note", "w1", text), (1, "", f"workers: {msg}\n"))
                self.assertEqual(self.bytes(), before)

    def test_note_forget_and_stop_need_an_entry_killing_nothing(self):
        for cmd in (["note", "w9", "x"], ["forget", "w9"], ["stop", "w9"]):
            with self.subTest(cmd=cmd[0]):
                before = self.seed(MGR, w1=entry())
                server = Server(sessions={"w9": opts(), "w1": opts(SID)})
                self.assertEqual(self.run_main(server, *cmd), (1, "", "workers: w9 not in roster\n"))
                self.assertEqual((self.bytes(), server.ran("kill-session")), (before, []))

    def test_note_forget_and_stop_check_the_name(self):
        for cmd in (["note", "a b", "x"], ["forget", "a b"], ["stop", "a b"]):
            with self.subTest(cmd=cmd[0]):
                before = self.seed(MGR, w1=entry())
                self.assertEqual(self.run_main(Server(), *cmd),
                                 (1, "", "workers: bad name 'a b': use [A-Za-z0-9_-]+\n"))
                self.assertEqual(self.bytes(), before)

    def test_forget_removes_an_entry_no_session_keeps(self):
        self.seed(MGR, w1=entry(state="gone"), r1=entry("role", tui="r1-tui"), w2=entry())
        self.assertEqual(self.run_main(Server(sessions={"w2": opts()}), "forget", "w1"), (0, "", ""))
        self.assertEqual(self.run_main(Server(sessions={"w2": opts()}), "forget", "r1"), (0, "", ""))
        self.assertEqual(self.entries(), {"w2": entry()})

    def test_forget_refuses_a_live_entry_writing_nothing(self):
        """A worker by its session; a role or pipeline by its driver (its name) or its tui."""
        entries = {"w1": entry(), "r1": entry("role", tui="r1-tui"), "r2": entry("role", tui="r2-tui"),
                   "p1": entry("pipeline", tui="p1-tui")}
        server = Server(sessions={"w1": opts(), "r1": opts(), "r2-tui": opts(), "p1-tui": opts()})
        for name, session in (("w1", "w1"), ("r1", "r1"), ("r2", "r2-tui"), ("p1", "p1-tui")):
            with self.subTest(name):
                before = self.seed(MGR, **entries)
                self.assertEqual(self.run_main(server, "forget", name),
                                 (1, "", f"workers: {name}: session {session} is live\n"))
                self.assertEqual(self.bytes(), before)

    def test_forget_after_a_failed_list_sessions_writes_nothing(self):
        before = self.seed(MGR, w1=entry(state="gone"))
        self.assertEqual(self.run_main(Server(fail={"list-sessions": "boom\n"}), "forget", "w1"),
                         (1, "", "workers: tmux list-sessions: boom\n"))
        self.assertEqual(self.bytes(), before)


class StopTest(RosterCase):
    """workers.py stop, mgr the holder unless a test says otherwise (its refusals: RosterCommandsTest)."""

    def stopped(self, server, name, *argv):
        """stop's (exit code, stdout, stderr), the targets it killed and the entries left."""
        res = self.run_main(server, "stop", name, *argv)
        return res, [c[3] for c in server.ran("kill-session")], set(self.entries())

    def test_a_worker(self):
        self.seed(MGR, w1=entry(), w2=entry(sid=SID2))
        server = Server(sessions={"w1": opts(SID), "w2": opts(SID2)})
        self.assertEqual(self.stopped(server, "w1"), ((0, "", ""), ["=w1"], {"w2"}))
        self.assertEqual(set(server.sessions), {"mgr", "w2"})

    def test_a_role_or_pipeline_by_its_key_or_tui_its_driver_then_tui(self):
        for kind in ("role", "pipeline"):
            for name in ("d1", "t1"):
                with self.subTest(kind=kind, name=name):
                    self.seed(MGR, d1=entry(kind, tui="t1"), w1=entry())
                    server = Server(sessions={"d1": opts(), "t1": opts(), "w1": opts(SID)})
                    self.assertEqual(self.stopped(server, name), ((0, "", ""), ["=d1", "=t1"], {"w1"}))
                    self.assertEqual(set(server.sessions), {"mgr", "w1"})

    def test_sessions_gone_already(self):
        self.seed(MGR, w1=entry(state="gone"), d1=entry("role", tui="t1", state="finished"), d2=entry("role"),
                  w2=entry())
        for name, killed, left in (("w1", ["=w1"], {"d1", "d2", "w2"}), ("t1", ["=d1", "=t1"], {"d2", "w2"}),
                                   ("d2", ["=d2"], {"w2"})):
            with self.subTest(name=name):
                self.assertEqual(self.stopped(Server(), name), ((0, "", ""), killed, left))

    def test_a_session_outliving_its_kill_exits_1_keeping_the_entry(self):
        before = self.seed(MGR, w1=entry())
        server = Server(sessions={"w1": opts(SID)}, fail={"kill-session": "boom\n"})
        self.assertEqual(self.run_main(server, "stop", "w1"),
                         (1, "", "workers: w1: session w1 still live after tmux kill-session\n"))
        self.assertEqual(self.bytes(), before)

    def test_outside_tmux_without_a_live_holder(self):
        for holder in (None, OTHER):
            with self.subTest(holder=holder):
                self.seed(holder, w1=entry(), w2=entry(sid=SID2))
                server = Server(None, sessions={"w1": opts(SID)})
                self.assertEqual(self.stopped(server, "w1", "--manager", "mgr"), ((0, "", ""), ["=w1"], {"w2"}))
                self.assertEqual(json.loads(self.bytes())["holder"], holder)

    def test_a_stopped_entry_is_not_resumed(self):
        self.seed(MGR, w1=entry(), w2=entry(sid=SID2, resume=[PY, "/y/workers.py", "--go"]))
        self.assertEqual(self.run_main(Server(sessions={"w1": opts(SID)}), "stop", "w1"), (0, "", ""))
        server = Server()
        self.assertEqual(self.run_main(server, "attach", "--resume")[0], 0)
        self.assertEqual([argv for argv, _, _ in server.recoveries], [[PY, "/y/workers.py", "--go"]])


class ResumeTest(RosterCase):
    """workers.py attach --resume, mgr attaching: every live session shown (no reopen); the recovery commands run on
    Server."""
    RUN = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True,
           "errors": "replace"}

    def test_each_gone_entry_runs_its_recovery_argv_in_its_cwd_unlocked_no_shell_in_table_order(self):
        cwd, finished = os.path.join(self.dir, "it's a role"), os.path.join(self.dir, "finished")
        os.mkdir(finished)
        with open(os.path.join(finished, "run.jsonl"), "w") as f:
            f.write(result(outcome={"status": "done"}))
        entries = {"b": entry(cwd="/w/b", resume=[PY, "/x/b/workers.py", "start", "b"]),
                   "a": entry(cwd=cwd, resume=[PY, "/x/a/workers.py", "; rm -rf ~"]),
                   "B": entry("role", cwd=cwd, resume=[PY, "/x/drive.py", "--role", "pm"]),
                   "p1": entry("pipeline", cwd=cwd, resume=[PY, "/x/router.py", "--issue", "TASK-1"]),
                   "f1": entry("pipeline", cwd=finished), "live": entry(sid=SID2)}
        self.seed(MGR, **entries)
        results = {"/x/b/workers.py": (0, f"tui: session b: tmux attach -t '=b'\nb {SID}\n"),
                   "/x/a/workers.py": (0, "  a \n\n  \n"), "/x/drive.py": (0, "drive.py: session x\n"),
                   "/x/router.py": (0, "")}
        plain = Server(sessions={"live": opts(SID2)}, clients={"live": SHOWN})
        rc, listed, err = self.run_main(plain, "attach")
        self.assertEqual((rc, err, plain.recoveries), (0, "", []))
        server = Server(sessions={"live": opts(SID2)}, clients={"live": SHOWN}, results=results, lock=self.lock)
        self.assertEqual(self.run_main(server, "attach", "--resume"),
                         (0, listed + nl("resumed B: exit 0: drive.py: session x", "resumed a: exit 0: a",
                                         f"resumed b: exit 0: b {SID}", "resumed p1: exit 0"), ""))
        ran = {name: manager.recovery(entries[name]) for name in ("B", "a", "b", "p1")}
        self.assertEqual(server.recoveries, [(argv, {"cwd": entries[name]["cwd"], **self.RUN}, True)
                                             for name, argv in ran.items()])
        printed = dict(line.removeprefix("resume ").split(": ", 1) for line in listed.splitlines()
                       if line.startswith("resume "))
        self.assertEqual({n: shlex.split(cmd) for n, cmd in printed.items()},
                         {**ran, "B": ["cd", cwd, "&&", *ran["B"]]})

    def test_a_failing_recovery_stops_none_and_attach_exits_1(self):
        refusal = "router.py: TASK-1 has a live agent run: tmux attach -t '=agent-pm-pm-TASK-1'"
        self.seed(MGR, a1=entry(resume=[PY, "/x/a1/workers.py"]), a2=entry(resume=[PY, "/x/a2/workers.py"]),
                  a3=entry("role", sid=None), a4=entry(resume=[PY, "/x/a4/workers.py"]),
                  p1=entry("pipeline", resume=[PY, "/x/router.py"]), w1=entry(resume=[PY, "/x/w1/workers.py"]))
        results = {"/x/a1/workers.py": (1, "workers: a1: session ended at once\n"),
                   "/x/a2/workers.py": FileNotFoundError(errno.ENOENT, "No such file or directory"),
                   "/x/a4/workers.py": ValueError("embedded null byte"),
                   "/x/router.py": (1, refusal + "\n")}
        server = Server(results=results)
        rc, out, err = self.run_main(server, "attach", "--resume")
        self.assertEqual((rc, err), (1, ""))
        self.assertTrue(out.endswith(tail(self.mdir) + nl(
            "resumed a1: exit 1: workers: a1: session ended at once",
            "resumed a2: not run: No such file or directory",
            "resumed a3: not run: no sid",
            "resumed a4: not run: ValueError: embedded null byte",
            f"resumed p1: exit 1: {refusal}",
            "resumed w1: exit 0")), out)
        self.assertEqual([argv[1] for argv, _, _ in server.recoveries],
                         ["/x/a1/workers.py", "/x/a2/workers.py", "/x/a4/workers.py", "/x/router.py",
                          "/x/w1/workers.py"])

    def test_one_failed_recovery_of_any_kind_exits_1(self):
        cases = {"exit 1": ((1, "boom\n"), "exit 1: boom"), "exit 255": ((255, ""), "exit 255"),
                 "an OSError": (PermissionError(errno.EACCES, "denied"), "not run: denied"),
                 "a ValueError": (ValueError("embedded null byte"), "not run: ValueError: embedded null byte"),
                 "a TypeError": (TypeError("bad argv"), "not run: TypeError: bad argv"),
                 "no sid": (None, "not run: no sid")}
        for why, (failure, line) in cases.items():
            with self.subTest(why):
                bad = entry("role", sid=None) if failure is None else entry(resume=[PY, "/x/bad/workers.py"])
                self.seed(MGR, bad=bad, ok=entry(resume=[PY, "/x/ok/workers.py"]))
                server = Server(results={} if failure is None else {"/x/bad/workers.py": failure})
                rc, out, err = self.run_main(server, "attach", "--resume")
                self.assertEqual((rc, err), (1, ""))
                self.assertTrue(out.endswith(tail(self.mdir) + nl(f"resumed bad: {line}", "resumed ok: exit 0")), out)

    def test_a_waiting_run_is_left_alone(self):
        asking = os.path.join(self.dir, "asking")
        os.mkdir(asking)
        with open(os.path.join(asking, "run.jsonl"), "w") as f:
            f.write(result(outcome={"status": "needs_input", "questions": ["Which?"]}))
        self.seed(MGR, r1=entry("role", cwd=asking), p1=entry("pipeline", cwd=asking),
                  w1=entry(cwd=asking, resume=[PY, "/x/w1/workers.py"]))
        server = Server()
        rc, out, err = self.run_main(server, "attach", "--resume")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, HEADER + nl(f"p1\tpipeline\t{SID}\twaiting\t-\t{asking}\t-",
                                          f"r1\trole\t{SID}\twaiting\t-\t{asking}\t-",
                                          f"w1\tworker\t{SID}\tgone\t-\t{asking}\t-",
                                          f"resume w1: {PY} /x/w1/workers.py") + tail(self.mdir)
                         + "resumed w1: exit 0\n")
        self.assertEqual([argv for argv, _, _ in server.recoveries], [[PY, "/x/w1/workers.py"]])
        self.assertEqual({n: e["state"] for n, e in self.entries().items()},
                         {"p1": "waiting", "r1": "waiting", "w1": "gone"})


class CursorTest(RosterCase):
    """attach and next-event record the cursor; attach rotates and prints the backlog and the arm commands. mgr holds
    the lease; next_event is a fake recording (events, after, the roster's cursor while it runs) in self.waits."""
    BIG = SPAN * 11 // 10   # the lines of a 1.1 MiB events file
    ROTATE = "workers: rotate: run workers.py attach, then arm with its N and GEN\n"

    def setUp(self):
        super().setUp()
        self.mevents = os.path.join(self.mdir, "events")
        self.old = self.mevents + ".1"
        self.waits = []
        p = mock.patch.object(workers, "next_event", side_effect=self.wait)
        p.start()
        self.addCleanup(p.stop)

    def wait(self, events, after):
        self.waits.append((events, after, json.loads(self.bytes())["cursor"]))
        return after + 1, "12:00:00 w1 outcome done"

    def fill(self, data, cursor=0, gen=0):
        """mgr's events file holding data, its roster held by mgr at cursor and gen; returns the roster's bytes."""
        before = self.seed(MGR, cursor=cursor, gen=gen)
        with open(self.mevents, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        return before

    def read(self, path=None):
        with open(path or self.mevents, "rb") as f:
            return f.read()

    def cursor(self):
        r = json.loads(self.bytes())
        return r["cursor"], r["gen"]

    def attach(self, *argv, own="mgr"):
        return self.run_main(Server(own), "attach", "--manager", "mgr", *argv)

    def next_event(self, after, gen=0):
        return self.run_main(Server(), "next-event", "--after", str(after), "--gen", str(gen))

    def test_attach_prints_the_backlog_after_the_cursor_then_n_gen_and_the_arm_commands(self):
        events = [f"12:00:0{i} w1 done" for i in range(1, 6)]
        self.fill(nl(*events), cursor=3)
        me = shlex.quote(os.path.abspath(workers.__file__))
        self.assertEqual(self.attach(), (0, HEADER + nl(
            f"4 {events[3]}", f"5 {events[4]}", "N=3 GEN=0", f"monitor: tail -n +4 -F {self.mevents}",
            f"monitor expired: python3 {me} attach --manager mgr --after LINE --gen 0 --resume",
            f"next-event: python3 {me} next-event --manager mgr --after 3 --gen 0"), ""))
        self.assertEqual(self.cursor(), (3, 0))

    def test_the_backlog_skips_a_non_event_line_but_counts_it(self):
        self.fill(b"12:00:01 w1 done\ngarbage\n\n12:00:04 w1 caf\xc3\xa9 \xff\n12:00:05 w2 blocked\n12:00:06 w1 do",
                  cursor=1)
        self.assertEqual(self.attach(), (0, HEADER + tail(self.mdir, 1, 0, "4 12:00:04 w1 café �",
                                                          "5 12:00:05 w2 blocked"), ""))

    def test_attach_quotes_the_paths_it_prints(self):
        home = os.path.join(self.dir, "it's a home")
        os.mkdir(home)
        script = os.path.join(self.dir, "my scripts", "workers.py")
        with mock.patch.dict(os.environ, {"HOME": home}), mock.patch.object(workers, "__file__", script):
            rc, out, err = self.attach()
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual([shlex.split(line.split(": ", 1)[1]) for line in out.splitlines()[-3:]],
                         [["tail", "-n", "+1", "-F", os.path.join(home, ".agent-pm", "managers", "mgr", "events")],
                          ["python3", script, "attach", "--manager", "mgr", "--after", "LINE", "--gen", "0", "--resume"],
                          ["python3", script, "next-event", "--manager", "mgr", "--after", "0", "--gen", "0"]])

    def test_attach_after_records_the_cursor_which_only_moves_forward(self):
        lines = numbered(9).splitlines()
        self.fill(numbered(9))
        self.assertEqual(self.attach("--after", "7", "--gen", "0"),
                         (0, HEADER + tail(self.mdir, 7, 0, f"8 {lines[7]}", f"9 {lines[8]}"), ""))
        self.assertEqual(self.next_event(5), (0, "6 12:00:00 w1 outcome done\n", ""))
        self.assertEqual((self.cursor(), self.waits), ((7, 0), [(self.mevents, 5, 7)]))
        self.assertEqual(self.attach()[1].splitlines()[-4], "N=7 GEN=0")
        self.assertEqual(self.cursor(), (7, 0))

    def test_attach_refuses_stale_line_numbers_writing_nothing(self):
        cases = {"an old gen": (["--after", "7", "--gen", "0"],
                                "stale line numbers (gen 0, now 1): run workers.py attach"),
                 "past the end": (["--after", "10", "--gen", "1"],
                                  f"line 10 is past the end of {self.mevents} (9 lines): run workers.py attach")}
        for why, (argv, msg) in cases.items():
            with self.subTest(why):
                before = self.fill(numbered(9), cursor=2, gen=1)
                self.assertEqual(self.attach(*argv), (1, "", f"workers: {msg}\n"))
                self.assertEqual(self.bytes(), before)

    def test_attach_after_and_gen_go_together_as_numbers(self):
        alone = "workers.py: error: --after and --gen go together\n"
        for argv, want in ((["--after", "3"], alone), (["--gen", "0"], alone), (["--after", "end", "--gen", "0"], None),
                           (["--after", "-1", "--gen", "0"], None), (["--after", "1", "--gen", "x"], None)):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    workers.main(["attach", "--manager", "mgr", *argv])
                self.assertEqual(cm.exception.code, 2)
                if want:
                    self.assertTrue(err.getvalue().endswith(want), err.getvalue())
                self.assertEqual(os.listdir(self.agent_pm), [])

    def test_attach_rotates_keeping_the_lines_after_the_cursor(self):
        data = numbered(self.BIG)
        backlog = [f"{i} {line}" for i, line in enumerate(data.splitlines()[-3:], 1)]
        cases = {"at the cursor": (self.BIG - 3, []),
                 "--after below the cursor": (self.BIG - 3, ["--after", "5", "--gen", "0"]),
                 "--after recorded first": (0, ["--after", str(self.BIG - 3), "--gen", "0"])}
        for why, (cursor, argv) in cases.items():
            with self.subTest(why):
                self.sleep.reset_mock()
                self.fill(data, cursor)
                ino = os.lstat(self.mevents).st_ino
                self.assertEqual(self.attach(*argv), (0, HEADER + tail(self.mdir, 0, 1, *backlog), ""))
                self.assertEqual(self.cursor(), (0, 1))
                self.assertEqual((os.lstat(self.old).st_ino, self.read(self.old)), (ino, data.encode()))
                self.assertEqual(self.read(), "".join(data.splitlines(keepends=True)[-3:]).encode())
                self.sleep.assert_called_once_with(manager.SETTLE)
        before = self.bytes()
        self.assertEqual(self.attach("--after", "2", "--gen", "0"),
                         (1, "", "workers: stale line numbers (gen 0, now 1): run workers.py attach\n"))
        self.assertEqual(self.bytes(), before)

    def test_attach_refuses_a_symlinked_events_1_renaming_nothing(self):
        data = numbered(self.BIG).encode()
        before = self.fill(data, self.BIG - 3)
        os.symlink(os.path.join(self.dir, "target"), self.old)
        self.assertEqual(self.attach(), (1, "", f"workers: {self.old}: not a regular file owned by you\n"))
        self.assertEqual((self.bytes(), self.read(), os.path.islink(self.old)), (before, data, True))

    def test_a_failed_list_sessions_with_a_rotation_due_renames_nothing(self):
        data = numbered(self.BIG).encode()
        before = self.fill(data, self.BIG - 3)
        self.assertEqual(self.run_main(Server(fail={"list-sessions": "boom\n"}), "attach"),
                         (1, "", "workers: tmux list-sessions: boom\n"))
        self.assertEqual((self.bytes(), self.read(), os.path.exists(self.old)), (before, data, False))

    def test_attach_after_a_failed_copy_still_numbers_the_new_events(self):
        data = numbered(self.BIG).encode()
        self.fill(data, self.BIG - 3)
        real = os.open

        def open_(path, flags, *args, **kw):
            if path == self.mevents and flags & os.O_APPEND and flags & os.O_NONBLOCK:
                raise OSError(errno.EIO, "Input/output error")
            return real(path, flags, *args, **kw)

        with mock.patch("os.open", open_):
            rc, out, err = self.attach()
        self.assertEqual((rc, out), (0, HEADER + tail(self.mdir, 0, 1)))
        self.assertEqual(err, f"manager: rotate: {self.mevents}: Input/output error: lines after {self.BIG - 3} of "
                              f"{self.old} not copied\n")
        self.assertEqual((self.cursor(), self.read(self.old), self.read()), ((0, 1), data, b""))

    def test_next_event_records_the_line_before_it_waits(self):
        self.fill(numbered(5) + "12:00:00 w6 do")
        self.assertEqual(self.next_event(3), (0, "4 12:00:00 w1 outcome done\n", ""))
        self.assertEqual(self.next_event("end"), (0, "6 12:00:00 w1 outcome done\n", ""))
        self.assertEqual(self.waits, [(self.mevents, 3, 3), (self.mevents, 5, 5)])

    def test_next_event_refuses_stale_line_numbers_on_stdout_writing_nothing(self):
        cases = {"past the end": (9, 2, f"line 9 is past the end of {self.mevents} (5 lines): run workers.py attach"),
                 "an old gen": (3, 1, "stale line numbers (gen 1, now 2): run workers.py attach")}
        for why, (after, gen, msg) in cases.items():
            with self.subTest(why):
                before = self.fill(numbered(5), cursor=2, gen=2)
                self.assertEqual(self.next_event(after, gen), (1, f"workers: {msg}\n", ""))
                self.assertEqual(self.bytes(), before)
        self.assertEqual(self.waits, [])

    def test_next_event_records_then_refuses_when_a_rotation_is_due(self):
        data = numbered(self.BIG).encode()
        self.fill(data)
        self.assertEqual(self.next_event(self.BIG - 3), (1, self.ROTATE, ""))
        self.assertEqual((self.cursor(), self.waits, self.read(), os.path.exists(self.old)),
                         ((self.BIG - 3, 0), [], data, False))

    def test_next_event_waits_on_exactly_events_max_bytes(self):
        self.fill(numbered(SPAN))
        self.assertEqual(self.next_event(SPAN), (0, f"{SPAN + 1} 12:00:00 w1 outcome done\n", ""))
        self.assertEqual(self.waits, [(self.mevents, SPAN, SPAN)])

    def test_unhandled_lines_past_events_max_rotate_only_once_half_is_handled(self):
        """events over EVENTS_MAX, all of it after the cursor: next-event waits and attach keeps the file until the
        handled bytes reach EVENTS_MAX // 2."""
        data = numbered(SPAN + 1).encode()
        self.fill(data)
        half = SPAN // 2
        for k in (1, 2, 3, half - 1):
            with self.subTest(k=k):
                self.assertEqual(self.next_event(k), (0, f"{k + 1} 12:00:00 w1 outcome done\n", ""))
        rc, out, err = self.attach()
        self.assertEqual((rc, err, out.splitlines()[-4]), (0, "", f"N={half - 1} GEN=0"))
        self.assertEqual((self.read(), os.path.exists(self.old)), (data, False))
        self.assertEqual(self.next_event(half), (1, self.ROTATE, ""))
        self.assertEqual((self.cursor(), self.read()), ((half, 0), data))

    def test_next_event_refuses_a_fifo_events_on_stderr(self):
        before = self.seed(MGR)
        os.mkfifo(self.mevents)
        with within():
            self.assertEqual(self.next_event(0), (1, "", f"workers: {self.mevents}: not a regular file owned by you\n"))
        self.assertEqual(self.bytes(), before)

    def test_an_interrupted_next_event_leaves_its_line_for_the_next_manager(self):
        """FR-15: next-event --after 12 interrupted, the lease released, another session's attach resumes after 12."""
        data = numbered(15)
        self.fill(data)
        with mock.patch.object(workers, "next_event", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.next_event(12)
        self.assertEqual(self.run_main(Server(), "release"), (0, f"workers: released {self.mdir}\n", ""))
        backlog = [f"{i} {line}" for i, line in enumerate(data.splitlines()[12:], 13)]
        self.assertEqual(self.attach(own="m2"), (0, HEADER + tail(self.mdir, 12, 0, *backlog), ""))
        self.assertEqual(json.loads(self.bytes())["holder"]["session"], "m2")


if __name__ == "__main__":
    unittest.main()
