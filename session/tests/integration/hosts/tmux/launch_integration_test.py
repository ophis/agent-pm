"""TmuxHost preserves literal launch data and terminal paste bytes on a private server."""

import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

from agent_pm.session import DriverError, Launch
from agent_pm.session.engine.types import Paste
from agent_pm.session.hosts.tmux import TmuxHost

from session.tests import live_tmux
from session.tests.case import Case

WAIT = 10


class LaunchTest(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start("manager")
        self.enterContext(mock.patch.dict(os.environ, self.server.env(), clear=True))
        self.host = TmuxHost("tmux")
        self.handles = []
        self.addCleanup(self.close_all)

    def close_all(self):
        for process in self.handles:
            process.close()

    def poll(self, check):
        deadline = time.monotonic() + WAIT
        while not (found := check()):
            if time.monotonic() >= deadline:
                self.fail("launch observation timed out")
            time.sleep(0.02)
        return found

    def launch(self, kind, name, launch):
        process = getattr(self.host, kind)(name, launch)
        self.handles.append(process)
        return process

    def test_literal_arguments_supplied_path_cwd_and_environment_reach_both_kinds(self):
        bindir = Path(self.home, "bin with spaces")
        bindir.mkdir()
        script = bindir / "literal-program"
        script.write_text(
            f"#!{sys.executable} -I\n"
            "import json, os, sys, time\n"
            "out, gate, *data = sys.argv[1:]\n"
            "with open(out, 'w') as f: json.dump({'argv': data, 'cwd': os.getcwd(), 'env': dict(os.environ)}, f)\n"
            "while not os.path.exists(gate): time.sleep(0.01)\n"
        )
        script.chmod(0o700)
        data = ["a b", "雪", "two\nlines", "-leading", ";", "$(echo literal)", "--"]
        for kind in ("detach", "terminal"):
            with self.subTest(kind=kind):
                out, gate = Path(self.home, kind + ".json"), Path(self.home, kind + ".gate")
                env = {
                    "PATH": str(bindir),
                    "PYTHONPATH": "literal import roots",
                    "AGENT_PM_EVENTS": "literal events",
                    "TMUX": "supplied-server",
                    "PWD": "supplied-pwd",
                    "TERM": "supplied-term",
                    "LC_ALL": "C",
                }
                process = self.launch(
                    kind,
                    kind,
                    Launch((script.name, str(out), str(gate), *data), cwd=self.home, env=env),
                )
                self.poll(lambda out=out: out.exists() and out.stat().st_size)
                got = json.loads(out.read_text())
                self.assertEqual(got["argv"], data)
                self.assertTrue(os.path.samefile(got["cwd"], self.home))
                self.assertEqual(got["env"], env)
                self.assertTrue(process.alive())
                gate.touch()
                self.assertTrue(process.wait_exit(WAIT))

    def test_long_unicode_multiline_paste_is_literal_and_does_not_submit(self):
        text = ("雪 a b\n" * 3000) + "literal ; $(echo x)"
        payload = text.encode()
        ready, output = Path(self.home, "ready"), Path(self.home, "paste")
        code = (
            "import os, sys, tty\n"
            "tty.setraw(0)\n"
            "open(sys.argv[1], 'w').close()\n"
            "data = b''\n"
            "while len(data) < int(sys.argv[3]): data += os.read(0, int(sys.argv[3]) - len(data))\n"
            "with open(sys.argv[2], 'wb') as f: f.write(data)\n"
            "while True: os.read(0, 1)\n"
        )
        terminal = self.launch(
            "terminal",
            "paste",
            Launch(
                (sys.executable, "-I", "-c", code, str(ready), str(output), str(len(payload))),
                cwd=self.home,
            ),
        )
        self.poll(ready.exists)
        before = self.server.tmux("list-buffers").stdout
        terminal.send(Paste(text))
        self.poll(lambda: output.exists() and output.stat().st_size == len(payload))
        self.assertEqual(output.read_bytes(), payload)
        self.assertTrue(terminal.alive())
        self.assertEqual(self.server.tmux("list-buffers").stdout, before)

    def test_stale_terminal_cannot_send_to_a_same_name_replacement(self):
        launch = Launch((sys.executable, "-I", "-c", "import time; time.sleep(30)"), cwd=self.home)
        old = self.launch("terminal", "same", launch)
        old.close()
        new = self.launch("terminal", "same", launch)
        with self.assertRaises(DriverError):
            old.send(Paste("old input"))
        old.kill()
        old.close()
        self.assertTrue(old.wait_exit(0))
        self.assertTrue(new.alive())
