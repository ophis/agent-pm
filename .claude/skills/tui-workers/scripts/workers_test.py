import contextlib
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

KW = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}
tui_claude = workers.tui_claude


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


def decorate_calls(events):
    """The tmux calls tui_claude.decorate makes for worker w1."""
    fake = Fake()
    tui_claude.decorate("w1", events, proc=fake)
    return fake.calls


class Fake:
    """Records proc calls; `new_err` makes new-session fail. On new-session it reads the handover file tui_claude wrote
    and unlinks it as the pane's wrapper would."""

    def __init__(self, new_err=""):
        self.new_err = new_err
        self.calls, self.kwargs = [], []
        self.store, self.respawn_rc, self.respawn_err = {}, 0, ""
        self.fail_set, self.oserror, self.handover = None, None, None

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
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
        if argv[1] == "respawn-pane":
            return subprocess.CompletedProcess(argv, self.respawn_rc, "", self.respawn_err)
        if argv[1] == "new-session":
            path = argv[argv.index(";") - 1]
            with open(path) as f:
                self.handover = json.load(f)
            os.unlink(path)
            if self.new_err:
                return subprocess.CompletedProcess(argv, 1, "", self.new_err)
        return subprocess.CompletedProcess(argv, 0, "", "")

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
        self.env = {"PATH": self.bin, "CLAUDE_CONFIG_DIR": "/cfg", "CLAUDE_FOO": "1", "HOME": "/h"}
        for k in workers.STRIP:
            self.env[k] = "x"
        spy = mock.patch.object(tui_claude, "start", wraps=tui_claude.start)
        self.tui = spy.start()
        self.addCleanup(spy.stop)

    def start(self, fake, name="w1", **kw):
        kw.setdefault("cwd", self.dir)
        kw.setdefault("env", self.env)
        return workers.start(name, self.events, proc=fake, **kw)

    def layout(self):
        kw = self.tui.call_args.kwargs
        return kw["beside"], kw["split"]

    def test_tui_claude_in_process(self):
        self.assertEqual(os.path.realpath(tui_claude.__file__), os.path.join(os.path.realpath(workers.ROOT), "core",
                                                                             "src", "tui_claude.py"))

    def test_tui_claude_call(self):
        fake = Fake()
        sid = self.start(fake, prompt="do it", flags=("--model", "m"))
        self.assertEqual(str(uuid.UUID(sid)), sid)
        argv = [self.claude, "--session-id", sid, "--name", "w1", "--model", "m", "--", "do it"]
        env = {k: v for k, v in self.env.items() if k not in workers.STRIP}
        self.tui.assert_called_once_with("w1", argv, cwd=self.dir, env=env, events=self.events, split=None,
                                         beside=None, proc=fake)
        self.assertEqual(fake.handover["argv"], tui_claude.with_hooks(argv, self.events))

    def test_no_prompt(self):
        self.start(Fake(), flags=("--x",))
        self.assertEqual(self.tui.call_args.args[1][-1], "--x")

    def test_env_cwd_in_the_handover(self):
        fake = Fake()
        self.start(fake)
        self.assertEqual(fake.handover["cwd"], self.dir)
        env = fake.handover["env"]
        for k in workers.STRIP:
            self.assertNotIn(k, env)
        self.assertEqual({k: env[k] for k in ("CLAUDE_FOO", "CLAUDE_CONFIG_DIR", "HOME")},
                         {"CLAUDE_FOO": "1", "CLAUDE_CONFIG_DIR": "/cfg", "HOME": "/h"})
        self.assertIn("CLAUDECODE", self.env)

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
        self.assertEqual(set(opts), {"@sid", "@cwd", "@events", "@claude", "@env", "@flags", "status"})
        self.assertEqual(opts["@sid"], sid)
        self.assertEqual(opts["@cwd"], self.dir)
        self.assertEqual(opts["@events"], self.events)
        self.assertEqual(opts["@claude"], self.claude)
        self.assertEqual(json.loads(opts["@env"]), {"PATH": self.bin, "CLAUDE_CONFIG_DIR": "/cfg"})
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
        for fail in ("@sid", "@env", "@flags"):
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

    def test_env_without_config_dir(self):
        del self.env["CLAUDE_CONFIG_DIR"]
        fake = Fake()
        self.start(fake)
        self.assertEqual(json.loads(fake.options()["@env"]), {"PATH": self.bin})

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

    def test_kill_failure_ignored(self):
        fake = Fake()
        fake.fail_set = "@sid"
        fake.oserror = lambda argv: argv[1] == "kill-session"
        with self.assertRaisesRegex(workers.WorkersError, "undone"):
            self.start(fake)

    def test_layout_passed_through(self):
        for kw in ({}, {"beside": "x"}, {"split": "below"}, {"beside": "x", "split": "right"}):
            with self.subTest(**kw):
                fake = Fake()
                self.start(fake, **kw)
                self.assertEqual(self.layout(), (kw.get("beside"), kw.get("split")))
                self.assertNotIn("list-sessions", [c[1] for c in fake.calls])

    def test_bad_beside(self):
        fake = Fake()
        self.assertIn("bad name 'a b'", self.assert_fails(fake, beside="a b"))
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

    def started(self, fake, flags=("--model", "m c")):
        return workers.start("w1", self.events, cwd=self.dir, flags=flags, env=self.env, proc=fake)


class RestartTest(WorkerCase):
    def test_argv_and_cmd(self):
        fake = Fake()
        sid = self.started(fake)
        cmd = workers.restart("w1", proc=fake)
        argv = fake.calls[-1]
        self.assertEqual(argv, ["tmux", "respawn-pane", "-k", "-t", "=w1:", "-c", self.dir, cmd])
        self.assertEqual(fake.kwargs[-1]["stdin"], subprocess.DEVNULL)
        unset = [w for v in workers.STRIP for w in ("-u", v)]
        resumed = tui_claude.with_hooks([self.claude, "--resume", sid, "--name", "w1", "--model", "m c"], self.events)
        self.assertEqual(shlex.split(cmd), ["env", *unset, f"PATH={self.env['PATH']}",
                                            f"CLAUDE_CONFIG_DIR={self.env['CLAUDE_CONFIG_DIR']}", *resumed])
        self.assertEqual(resumed[1:3], ["--settings", tui_claude.hooks(self.events)])

    def test_own_settings_merged_with_the_hooks(self):
        own = {"type": "command", "command": "mine"}
        flags = ("--settings", json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [own]}]}}))
        fake = Fake()
        self.started(fake, flags=flags)
        words = shlex.split(workers.restart("w1", proc=fake))
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
            workers.restart("w1", proc=fake)
        self.assertNotIn("respawn-pane", [c[1] for c in fake.calls])

    def test_redecorates_and_clears_state_before_respawn(self):
        fake = Fake()
        self.started(fake)
        n = len(fake.calls)
        workers.restart("w1", proc=fake)
        tail = [c for c in fake.calls[n:] if c[1] != "show-options"]
        self.assertEqual(tail[:-1], [*decorate_calls(self.events), ["tmux", "set-option", "-t", "=w1:", "@state", ""]])
        self.assertEqual(tail[-1][1], "respawn-pane")

    def test_decoration_failure_no_respawn(self):
        fake = Fake()
        self.started(fake)
        fake.fail_set = "pane-died"
        with self.assertRaisesRegex(workers.WorkersError, "set boom"):
            workers.restart("w1", proc=fake)
        self.assertNotIn("respawn-pane", [c[1] for c in fake.calls])

    def test_prints_cmd_to_stderr(self):
        fake = Fake()
        self.started(fake)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cmd = workers.restart("w1", proc=fake)
        self.assertEqual(err.getvalue(), cmd + "\n")

    def test_not_a_worker(self):
        fake = Fake()
        with self.assertRaisesRegex(workers.WorkersError, "w1: not a worker"):
            workers.restart("w1", proc=fake)
        self.assertEqual([c[1] for c in fake.calls if c[1] == "respawn-pane"], [])

    def test_corrupt_options_not_a_worker(self):
        for key, bad in (("@env", ""), ("@env", "{"), ("@env", "[]"), ("@env", '{"PATH": 1}'),
                         ("@flags", ""), ("@flags", "{}"), ("@flags", "[1]"), ("@flags", "null"),
                         ("@sid", ""), ("@claude", ""), ("@cwd", ""), ("@events", "")):
            fake = Fake()
            self.started(fake)
            fake.store[key] = bad
            with self.assertRaisesRegex(workers.WorkersError, "w1: not a worker", msg=f"{key}={bad!r}"):
                workers.restart("w1", proc=fake)
            self.assertNotIn("respawn-pane", [c[1] for c in fake.calls])

    def test_oserror_from_proc(self):
        fake = Fake()
        self.started(fake)
        fake.oserror = lambda argv: argv[1] == "respawn-pane"
        with self.assertRaises(workers.WorkersError):
            workers.restart("w1", proc=fake)

    def test_bad_name(self):
        fake = Fake()
        with self.assertRaises(workers.WorkersError):
            workers.restart("a b", proc=fake)
        self.assertEqual(fake.calls, [])

    def test_respawn_failure(self):
        fake = Fake()
        self.started(fake)
        fake.respawn_rc, fake.respawn_err = 1, "can't find pane\n"
        with self.assertRaisesRegex(workers.WorkersError, "can't find pane"):
            workers.restart("w1", proc=fake)

    def test_no_config_dir(self):
        del self.env["CLAUDE_CONFIG_DIR"]
        fake = Fake()
        self.started(fake, flags=())
        cmd = workers.restart("w1", proc=fake)
        self.assertNotIn("CLAUDE_CONFIG_DIR", cmd)
        self.assertIn(tui_claude.hooks(self.events), shlex.split(cmd))


SID = "0b6f1c2e-3d4a-4b5c-8d6e-7f8091a2b3c4"


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

    def test_start_beside_split(self):
        fake = Fake()
        with mock.patch.object(tui_claude, "start", wraps=tui_claude.start) as tui:
            rc, _, err = self.run_main(["start", "w1", "--events", self.events, "--cwd", self.dir, "--beside", "x",
                                        "--split", "below", "--", "--model", "m"], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual((tui.call_args.kwargs["beside"], tui.call_args.kwargs["split"]), ("x", "below"))
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
        rc, out, err = self.run_main(["restart", "w1"], fake)
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("--resume", err)

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


if __name__ == "__main__":
    unittest.main()
