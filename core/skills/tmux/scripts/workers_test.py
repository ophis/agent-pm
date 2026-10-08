import contextlib
import errno
import io
import json
import os
import shlex
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

KW = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}
tui_claude = workers.tui_claude
SID = "0b6f1c2e-3d4a-4b5c-8d6e-7f8091a2b3c4"
RUNNING_PANE, DEAD_PANE = "0  \n", "1 1 \n"
ERROR = "Error: --session-id can only be used with --continue or --resume if --fork-session is also specified."
DEAD_LINE = "Pane is dead (status 1, Wed Oct  7 19:54:08 2026)"
DEAD_TEXT = f"{ERROR}\n" + "\n" * 40 + f"{DEAD_LINE}\n" + "\n" * 10
DEAD_MSG = f"w1: claude exited 1 at once; its pane's last lines:\n{ERROR}\n{DEAD_LINE}"
REFUSED = ("--resume", "-r", "--session-id", "--continue", "-c", "--fork-session", "--from-pr", "--teleport")


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
        self.events = os.path.join(self.dir, "workers.events")
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
        return workers.start(name, self.events, proc=fake, **kw)

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
                                         split_from=None, status_line=False, proc=fake)
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

    def test_events_parent_missing(self):
        self.events = os.path.join(self.dir, "nope", "e")
        fake = Fake()
        self.assertIn("events file", self.assert_fails(fake))
        self.assertEqual(fake.new_sessions(), [])

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
                self.assertNotIn("list-sessions", [c[1] for c in fake.calls])

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
        self.events = os.path.join(self.dir, "it's events")
        self.env = {"PATH": self.bin + ":/a b", "CLAUDE_CONFIG_DIR": os.path.join(self.dir, "cfg")}
        no_sleep(self)

    def started(self, fake, flags=("--model", "m c")):
        return workers.start("w1", self.events, cwd=self.dir, flags=flags, env=self.env, proc=fake)


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
                             "list-clients": lambda argv: f"100 /dev/pts/1 %0 {sock}\n",
                             "list-panes": lambda argv: "%0\n%5\n" if argv[-1] == "#{pane_id}" else "0 /dev/pts/0 %0\n",
                             "split-window": lambda argv: next(new)})
        fake.oserror = lambda argv: argv[0] == "pgrep"
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"TMUX": f"{sock},42,0", "TMUX_PANE": "%0", "PATH": self.bin}, clear=True), \
                contextlib.redirect_stderr(err):
            for name in ("w1", "w2"):
                workers.start(name, self.events, cwd=self.dir, env=self.env, proc=fake)
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
        self.assertEqual(fake.calls[last + 1:], [["sleep", 0.5], status_call()] * 10)
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
    return json.dumps({"type": kind, "message": message}) + "\n"


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
    def run_main(self, argv, fake=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(workers.subprocess, "run", fake or Fake()), \
                mock.patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = workers.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_start(self):
        fake = Fake()
        rc, out, err = self.run_main(["start", "w1", "--events", self.events, "--cwd", self.dir,
                                      "--prompt", "go", "--", "--model", "sonnet", "--permission-mode", "default"],
                                     fake)
        self.assertEqual(rc, 0, err)
        sid = fake.store["@sid"]
        self.assertEqual(out, f"w1 {sid}\n")
        self.assertEqual(json.loads(fake.store["@flags"]), ["--model", "sonnet", "--permission-mode", "default"])
        self.assertEqual(fake.handover["argv"][-1], "go")

    def test_start_resume(self):
        fake = Fake()
        rc, out, err = self.run_main(["start", "w1", "--events", self.events, "--cwd", self.dir, "--resume", SID,
                                      "--", "--model", "m"], fake)
        self.assertEqual((rc, out), (0, f"w1 {SID}\n"), err)
        self.assertEqual(fake.store["@sid"], SID)
        self.assertEqual(fake.handover["argv"], tui_claude.with_hooks(
            [self.claude, "--resume", SID, "--name", "w1", "--model", "m"], self.events))

    def test_start_early_death_exit_1(self):
        fake = Fake()
        fake.spawned, fake.text = DEAD_PANE, DEAD_TEXT
        rc, out, err = self.run_main(["start", "w1", "--events", self.events, "--cwd", self.dir], fake)
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
        for argv, msg in ((["--resume", "abc"], "--resume abc: not a session id"),
                          (["--", "--model", "m", "-c"], "-c: workers.py picks the session; use start --resume <sid>"),
                          (["--resume", SID, "--", "--session-id=x"],
                           "--session-id: workers.py picks the session; use start --resume <sid>")):
            with self.subTest(argv=argv):
                fake = Fake()
                self.assertEqual(self.run_main(["start", "w1", "--events", self.events, *argv], fake),
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
        fake = Fake(results={"list-clients": lambda argv: f"100 /dev/ttys009 %9 {sock}\n" if argv[3] == "=x" else "",
                             "split-window": lambda argv: "%10\n"})
        fake.oserror = lambda argv: argv[0] == "pgrep"
        rc, _, err = self.run_main(["start", "w1", "--events", self.events, "--cwd", self.dir, "--split-from", "x",
                                    "--split", "below", "--", "--model", "m"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual([c for c in fake.calls if c[1] == "split-window"],
                         [["tmux", "split-window", "-d", "-v", "-P", "-F", "#{pane_id}", "-t", "%9",
                           "env", "-u", "TMUX", tmux, "-S", sock, "attach", "-t", "=w1"]])
        self.assertEqual(fake.store["@pane"], "%10")
        self.assertEqual(fake.handover["argv"][-2:], ["--model", "m"])

    def test_start_defaults_cwd(self):
        fake = Fake()
        rc, _, err = self.run_main(["start", "w1", "--events", self.events], fake)
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
        for argv in (["start", "w1"], ["start", "w1", "--events", self.events, "--split", "left"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                workers.main(argv)
            self.assertEqual(cm.exception.code, 2, argv)


def capture(name):
    """The tmux call `tui_claude.py read <name> --lines 40` makes."""
    return ["tmux", "capture-pane", "-p", "-J", "-t", f"={name}:", "-S", "-40"]


class ShowTest(WorkerCase):
    run_main = MainTest.run_main
    LINES = [f"line {i}" for i in range(1, 101)]

    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, "events")

    def next_event(self, event, fake, *show):
        with open(self.path, "w") as f:
            f.write(event + "\n")
        return self.run_main(["next-event", "--events", self.path, "--after", "0", *show], fake)

    def show(self, event, fake):
        return self.next_event(event, fake, "--show")

    def worker(self):
        fake = Fake()
        fake.store["@sid"] = SID
        fake.text = nl(*self.LINES)
        return fake

    def test_a_workers_done_prints_its_replys_first_80_lines(self):
        d = os.path.join(self.env["CLAUDE_CONFIG_DIR"], "projects", "p")
        os.makedirs(d)
        with open(os.path.join(d, SID + ".jsonl"), "w") as f:
            f.write(line("assistant", text("\n".join(self.LINES))))
        fake = self.worker()
        self.assertEqual(self.show("10:00:01 w1 done", fake), (0, nl("1 10:00:01 w1 done", *self.LINES[:80]), ""))
        self.assertNotIn("capture-pane", [c[1] for c in fake.calls])

    def test_a_role_runs_done_prints_its_pane(self):
        fake = Fake()
        fake.text = nl(*self.LINES)
        self.assertEqual(self.show("10:00:01 r-t-1a2b3c4d done", fake),
                         (0, nl("1 10:00:01 r-t-1a2b3c4d done", *self.LINES[-40:]), ""))
        self.assertEqual(fake.calls[-1], capture("r-t-1a2b3c4d"))

    def test_blocked_and_dead_print_the_pane(self):
        for kind in ("blocked", "dead"):
            with self.subTest(kind=kind):
                fake = self.worker()
                self.assertEqual(self.show(f"10:00:01 w1 {kind}", fake),
                                 (0, nl(f"1 10:00:01 w1 {kind}", *self.LINES[-40:]), ""))
                self.assertEqual(fake.calls, [capture("w1")])

    def test_other_events_print_only_the_line(self):
        fake = self.worker()
        self.assertEqual(self.show("10:00:01 r-t-1a2b3c4d-drive outcome done", fake),
                         (0, "1 10:00:01 r-t-1a2b3c4d-drive outcome done\n", ""))
        self.assertEqual(fake.calls, [])

    def test_without_show_only_the_line(self):
        fake = self.worker()
        self.assertEqual(self.next_event("10:00:01 w1 done", fake), (0, "1 10:00:01 w1 done\n", ""))
        self.assertEqual(fake.calls, [])

    def test_bad_name_exits_1_after_the_line_before_any_tmux_call(self):
        fake = Fake()
        self.assertEqual(self.show("10:00:01 a/b blocked", fake),
                         (1, "1 10:00:01 a/b blocked\n", "workers: bad name 'a/b': use [A-Za-z0-9_-]+\n"))
        self.assertEqual(fake.calls, [])

    def test_a_gone_session_exits_1_after_the_line(self):
        def gone(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, "", "can't find session: w1")
        self.assertEqual(self.show("10:00:01 w1 done", gone),
                         (1, "1 10:00:01 w1 done\n", "workers: tmux: can't find session: w1\n"))


def nl(*lines):
    return "".join(f"{x}\n" for x in lines)


class NextEventTest(unittest.TestCase):
    A, B, C = "10:00:01 w1 done", "10:00:02 w2 blocked", "10:00:03 w1 outcome needs_input"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(os.path.realpath(self.tmp.name), "events")

    def append(self, data):
        with open(self.path, "ab") as f:
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
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = workers.main(["next-event", *argv])
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
        self.append(nl(self.A, self.B))
        with self.sleeping():
            self.assertEqual(self.run_main("--events", self.path, "--after", "1"), (0, f"2 {self.B}\n", ""))
        with self.sleeping(lambda: self.append(nl(self.C))):
            self.assertEqual(self.run_main("--events", self.path, "--after", "end"), (0, f"3 {self.C}\n", ""))

    def test_main_error_exit(self):
        with self.sleeping():
            self.assertEqual(self.run_main("--events", self.tmp.name, "--after", "0"),
                             (1, "", f"workers: events file {self.tmp.name}: {os.strerror(errno.EISDIR)}\n"))

    def test_main_usage_error(self):
        for argv in (["--after", "-1"], ["--after", "x"], ["--after", "1.5"], ["--after", ""], []):
            with self.sleeping(), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                workers.main(["next-event", "--events", self.path, *argv])
            self.assertEqual(cm.exception.code, 2, argv)


if __name__ == "__main__":
    unittest.main()
