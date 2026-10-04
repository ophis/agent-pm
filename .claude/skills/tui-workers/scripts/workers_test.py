import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid

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

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.kwargs.append(kw)
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


if __name__ == "__main__":
    unittest.main()
