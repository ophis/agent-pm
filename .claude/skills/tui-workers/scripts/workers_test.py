import contextlib
import io
import json
import os
import re
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

EVENT_RE = re.compile(r"^\d\d:\d\d:\d\d w1 (done|blocked)\n$")
KW = {"capture_output": True, "text": True, "stdin": subprocess.DEVNULL}


def executable(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, 0o755)


class Fake:
    """Records proc calls; `sessions` is the list-sessions stdout, `tui_rc` tui.py's exit status."""

    def __init__(self, sessions=None, tui_rc=0, tui_err=""):
        self.sessions, self.tui_rc, self.tui_err = sessions, tui_rc, tui_err
        self.calls, self.kwargs = [], []
        self.store, self.respawn_rc, self.respawn_err = {}, 0, ""

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
        if argv[0] == "tmux" and argv[1] == "set-option":
            self.store[argv[4]] = argv[5]
        if argv[0] == "tmux" and argv[1] == "show-options":
            if argv[5] not in self.store:
                return subprocess.CompletedProcess(argv, 1, "", f"invalid option: {argv[5]}")
            return subprocess.CompletedProcess(argv, 0, self.store[argv[5]] + "\n", "")
        if argv[0] == "tmux" and argv[1] == "respawn-pane":
            return subprocess.CompletedProcess(argv, self.respawn_rc, "", self.respawn_err)
        if argv[0] == "tmux" and argv[1] == "list-sessions":
            if self.sessions is None:
                return subprocess.CompletedProcess(argv, 1, "", "no server running")
            return subprocess.CompletedProcess(argv, 0, self.sessions, "")
        if argv[0] == sys.executable:
            return subprocess.CompletedProcess(argv, self.tui_rc, "", self.tui_err)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def tui(self):
        return [c for c in self.calls if c[0] == sys.executable]

    def options(self):
        return {c[4]: c[5] for c in self.calls if c[0] == "tmux" and c[1] == "set-option"}


class HooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = os.path.join(self.tmp.name, "bin")
        os.mkdir(self.bin)
        executable(os.path.join(self.bin, "tmux"), "#!/bin/sh\necho w1\n")

    def run_hook(self, cmd, pane):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin"}
        if pane:
            env["TMUX_PANE"] = "%1"
        return subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, text=True)

    def commands(self, events):
        hooks = json.loads(workers.hooks(events))["hooks"]
        return hooks

    def test_shape(self):
        hooks = self.commands("/e")
        self.assertEqual(set(hooks), {"Stop", "Notification"})
        self.assertNotIn("matcher", hooks["Stop"][0])
        self.assertEqual(hooks["Notification"][0]["matcher"], "permission_prompt|elicitation_dialog|agent_needs_input")
        for name in hooks:
            inner = hooks[name][0]["hooks"]
            self.assertEqual(len(inner), 1)
            self.assertEqual(inner[0]["type"], "command")

    def test_lines_in_tmux(self):
        for sub in ("a b", "it's"):
            events = os.path.join(self.tmp.name, sub, "e")
            os.makedirs(os.path.dirname(events))
            hooks = self.commands(events)
            for name, word in (("Stop", "done"), ("Notification", "blocked")):
                res = self.run_hook(hooks[name][0]["hooks"][0]["command"], pane=True)
                self.assertEqual(res.returncode, 0, res.stderr)
                with open(events) as f:
                    lines = f.readlines()
                self.assertRegex(lines[-1], EVENT_RE)
                self.assertTrue(lines[-1].endswith(f" w1 {word}\n"))
            self.assertEqual(len(lines), 2)

    def test_outside_tmux_writes_nothing(self):
        events = os.path.join(self.tmp.name, "e")
        for name in ("Stop", "Notification"):
            cmd = self.commands(events)[name][0]["hooks"][0]["command"]
            self.assertEqual(self.run_hook(cmd, pane=False).returncode, 0)
        self.assertFalse(os.path.exists(events))


class StartTest(unittest.TestCase):
    def setUp(self):
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

    def start(self, fake, name="w1", **kw):
        kw.setdefault("cwd", self.dir)
        kw.setdefault("env", self.env)
        return workers.start(name, self.events, proc=fake, **kw)

    def test_paths(self):
        self.assertTrue(os.path.isfile(workers.TUI))
        self.assertEqual(os.path.basename(workers.TUI), "tui.py")

    def test_argv_no_siblings(self):
        fake = Fake()
        sid = self.start(fake, prompt="do it", flags=("--model", "m"))
        self.assertEqual(str(uuid.UUID(sid)), sid)
        (argv,) = fake.tui()
        self.assertEqual(argv[:4], [sys.executable, workers.TUI, "start", "w1"])
        self.assertEqual(argv[4], "--")
        self.assertEqual(argv[5:9], [self.claude, "--session-id", sid, "--name"])
        self.assertEqual(argv[9], "w1")
        self.assertEqual(argv[10], "--settings")
        self.assertEqual(argv[11], workers.hooks(self.events))
        self.assertEqual(argv[12:], ["--model", "m", "do it"])

    def test_no_prompt(self):
        fake = Fake()
        self.start(fake, flags=("--x",))
        self.assertEqual(fake.tui()[0][-1], "--x")

    def test_layout_beside_largest_started_attached_same_events(self):
        rows = [
            ("a", "1", self.events, "100"),
            ("b", "2", self.events, "300"),
            ("c", "0", self.events, "900"),
            ("d", "1", "/other", "800"),
            ("e", "1", "", ""),
            ("f", "1", self.events, "1000"[:2]),
        ]
        fake = Fake(sessions="".join("\t".join(r) + "\n" for r in rows))
        self.start(fake)
        argv = fake.tui()[0]
        self.assertEqual(argv[4:8], ["--beside", "b", "--split", "below"])
        self.assertEqual(argv[8], "--")
        self.assertEqual(fake.calls[0], ["tmux", "list-sessions", "-F",
                                         "#{session_name}\t#{session_attached}\t#{@events}\t#{@started}"])

    def test_no_server_no_layout(self):
        fake = Fake(sessions=None)
        self.start(fake)
        self.assertEqual(fake.tui()[0][4], "--")

    def test_env_cwd_kwargs(self):
        fake = Fake()
        self.start(fake)
        kw = fake.kwargs[fake.calls.index(fake.tui()[0])]
        self.assertEqual(kw["cwd"], self.dir)
        for k in workers.STRIP:
            self.assertNotIn(k, kw["env"])
        self.assertEqual(kw["env"]["CLAUDE_FOO"], "1")
        self.assertEqual(kw["env"]["CLAUDE_CONFIG_DIR"], "/cfg")
        self.assertEqual(kw["env"]["HOME"], "/h")
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertTrue(kw["capture_output"] and kw["text"])
        self.assertIn("CLAUDECODE", self.env)

    def test_relative_cwd_made_absolute(self):
        fake = Fake()
        old = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, old)
        self.start(fake, cwd=".")
        self.assertEqual(fake.kwargs[fake.calls.index(fake.tui()[0])]["cwd"], self.dir)
        self.assertEqual(fake.options()["@cwd"], self.dir)

    def test_set_options(self):
        fake = Fake()
        sid = self.start(fake, flags=("--model", "m"), prompt="p")
        opts = fake.options()
        self.assertEqual(set(opts), {"@sid", "@cwd", "@events", "@claude", "@env", "@flags", "@started"})
        self.assertEqual(opts["@sid"], sid)
        self.assertEqual(opts["@cwd"], self.dir)
        self.assertEqual(opts["@events"], self.events)
        self.assertEqual(opts["@claude"], self.claude)
        self.assertEqual(json.loads(opts["@env"]), {"PATH": self.bin, "CLAUDE_CONFIG_DIR": "/cfg"})
        self.assertEqual(json.loads(opts["@flags"]), ["--model", "m"])
        self.assertTrue(opts["@started"].isdigit())
        for c in fake.calls:
            if c[1] == "set-option":
                self.assertEqual(c[:4], ["tmux", "set-option", "-t", "=w1:"])
        self.assertLess(fake.calls.index(fake.tui()[0]), min(i for i, c in enumerate(fake.calls) if c[1] == "set-option"))

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
        self.assertEqual([c for c in fake.calls if len(c) > 1 and c[1] == "set-option"], [])
        return str(cm.exception)

    def test_bad_name(self):
        fake = Fake()
        self.assert_fails(fake, name="a b")
        self.assertEqual(fake.calls, [])

    def test_events_parent_missing(self):
        self.events = os.path.join(self.dir, "nope", "e")
        fake = Fake()
        self.assert_fails(fake)
        self.assertEqual(fake.tui(), [])

    def test_events_symlink(self):
        target = os.path.join(self.dir, "t")
        open(target, "w").close()
        os.symlink(target, self.events)
        fake = Fake()
        self.assert_fails(fake)
        self.assertEqual(fake.tui(), [])

    def test_claude_not_found(self):
        self.env["PATH"] = os.path.join(self.dir, "empty")
        fake = Fake()
        self.assertIn("claude", self.assert_fails(fake))
        self.assertEqual(fake.tui(), [])

    def test_tui_failure(self):
        fake = Fake(tui_rc=1, tui_err="tui: boom\n")
        self.assertIn("tui: boom", self.assert_fails(fake))


class WorkerCase(unittest.TestCase):
    def setUp(self):
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
    def setUp(self):
        super().setUp()
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_argv_and_cmd(self):
        fake = Fake()
        sid = self.started(fake)
        cmd = workers.restart("w1", proc=fake)
        argv = fake.calls[-1]
        self.assertEqual(argv, ["tmux", "respawn-pane", "-k", "-t", "=w1:", "-c", self.dir, cmd])
        self.assertEqual(fake.kwargs[-1]["stdin"], subprocess.DEVNULL)
        unset = [w for v in workers.STRIP for w in ("-u", v)]
        self.assertEqual(shlex.split(cmd), ["env", *unset, f"PATH={self.env['PATH']}",
                                            f"CLAUDE_CONFIG_DIR={self.env['CLAUDE_CONFIG_DIR']}", self.claude,
                                            "--resume", sid, "--name", "w1", "--settings",
                                            workers.hooks(self.events), "--model", "m c"])

    def test_agrees_with_start(self):
        fake = Fake()
        self.started(fake)
        (tui,) = fake.tui()
        cmd = shlex.split(workers.restart("w1", proc=fake))
        self.assertEqual(cmd[cmd.index("--settings") + 1], tui[tui.index("--settings") + 1])
        self.assertIn(tui[5], cmd)
        self.assertIn(f"PATH={self.env['PATH']}", cmd)
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.env['CLAUDE_CONFIG_DIR']}", cmd)

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
        self.assertEqual(shlex.split(cmd)[-1], workers.hooks(self.events))


SID = "0b6f1c2e-3d4a-4b5c-8d6e-7f8091a2b3c4"


def line(kind, *blocks):
    return json.dumps({"type": kind, "message": {"content": list(blocks)}}) + "\n"


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
        self.assertEqual(fake.tui()[0][-1], "go")

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
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            workers.main(["start", "w1"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
