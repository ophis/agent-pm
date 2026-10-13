"""session.engine.api.detach on a private tmux server (session/tests/live_tmux.py), os.environ that server's env: a child that
reports its blocked signals, unblocks them and runs until told to end; its session live then absent, its @opener, a
taken name, a cwd it cannot enter, a script whose interpreter is missing, an env execve refuses."""

import errno
import json
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_pm.session.engine import api
from agent_pm.session.engine.types import ConfigError, DriverError
from agent_pm.session.hosts.tmux import tmux_host

from session.tests import live_tmux
from session.tests.case import Case
from session.tests.detach_support import CHILD

MANAGER = "mgr"
# The child: writes its blocked signals to argv[1], unblocks them, then runs until argv[2] exists, at most 30 s.


class DetachTest(Case):
    def setUp(self):
        self.server = live_tmux.Server(self, self.home)
        self.server.start(MANAGER)
        patch = mock.patch.dict(os.environ, self.server.env(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.go = os.path.join(self.dir, "go")

    def argv(self, name: str) -> list[str]:
        return [sys.executable, "-I", "-c", CHILD, os.path.join(self.dir, f"{name}.json"), self.go]

    def detach(self, name: str, **kwargs) -> None:
        return self.driver.detach(name, self.argv(name), self.dir, self.server.env(), **kwargs)

    def blocked(self, name: str) -> set[int]:
        """The signals child `name` started with blocked, once it reports them."""
        path = os.path.join(self.dir, f"{name}.json")
        live_tmux.wait(lambda: os.path.exists(path), what=f"{name}'s report")
        with open(path) as f:
            return set(json.load(f))

    def gone(self) -> bool:
        # tmux on Linux (CI: 3.4) can leave an exited pane process unreaped until another child of the server exits
        self.server.tmux("run-shell", "-b", "true")
        return self.driver.sessions("d.*") == []

    def test_the_child_runs_with_the_signals_blocked_its_session_live_until_it_exits(self):
        self.assertIsNone(self.detach("d1", opener=MANAGER))
        self.assertEqual([r.name for r in self.driver.sessions("d1")], ["d1"])
        tmux_host.detach("d0", self.argv("d0"), cwd=self.dir, env=self.server.env())
        signals = {int(s) for s in api.SIGNALS}
        self.assertLessEqual(signals, self.blocked("d1"))
        self.assertEqual(signals & self.blocked("d0"), set())
        self.assertEqual(self.server.option("d1", "@opener"), MANAGER)
        with self.assertRaises(DriverError) as caught:
            self.driver.attach("d1")
        self.assertTrue(str(caught.exception).startswith("no tui session d1: "), caught.exception)
        with open(self.go, "w"):
            pass
        live_tmux.wait(self.gone, what="d0 and d1 absent once their children exit")

    def test_a_taken_name_is_refused_and_its_session_left_alone(self):
        self.detach("d1")
        self.blocked("d1")
        pid = self.server.tmux("display-message", "-p", "-t", "=d1:", "#{pane_pid}").stdout
        for name in ("d1", MANAGER):
            with self.subTest(name=name), self.assertRaises(DriverError) as caught:
                self.detach(name, opener="other")
            self.assertEqual(str(caught.exception), f"detach {name}: session {name} exists")
        self.assertEqual(
            self.server.tmux("display-message", "-p", "-t", "=d1:", "#{pane_pid}").stdout, pid
        )
        self.assertEqual([self.server.option(n, "@opener") for n in ("d1", MANAGER)], ["", ""])
        self.assertEqual([r.name for r in self.driver.sessions()], ["d1", MANAGER])

    def test_a_cwd_it_cannot_enter_is_refused_never_a_success(self):
        missing = os.path.join(self.dir, "missing")
        with self.assertRaises(ConfigError):
            self.driver.detach("d1", self.argv("d1"), missing, self.server.env())
        with self.assertRaisesRegex(tmux_host.TuiError, "^session d2 did not start$"):
            tmux_host.detach(
                "d2",
                self.argv("d2"),
                cwd=missing,
                env=self.server.env(),
                sleep=lambda seconds: time.sleep(seconds / 10),
            )
        live_tmux.wait(self.gone, what="no session left")

    def test_a_script_whose_interpreter_is_missing_fails_it_leaving_no_session(self):
        script = os.path.join(self.dir, "script")
        with open(script, "w") as f:
            f.write("#!/nonexistent/interpreter\n")
        os.chmod(script, 0o755)
        error = f"detach d1: cannot run {script}: {os.strerror(errno.ENOENT)}"
        with self.assertRaisesRegex(DriverError, "^" + re.escape(error) + "$"):
            self.driver.detach("d1", [script], self.dir, self.server.env())
        live_tmux.wait(self.gone, what="no session left")

    def test_an_env_execve_refuses_fails_it_leaving_no_session(self):
        with self.assertRaisesRegex(
            DriverError, rf"^detach d1: cannot run .+: {os.strerror(errno.EINVAL)}$"
        ):
            self.driver.detach("d1", self.argv("d1"), self.dir, {**self.server.env(), "X": "a\0b"})
        live_tmux.wait(self.gone, what="no session left")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "d1.json")))


if __name__ == "__main__":
    unittest.main()
