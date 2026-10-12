"""driver.api.detach on a private tmux server (new/tests/live_tmux.py), os.environ that server's env: a child that
reports its blocked signals, unblocks them and runs until told to end; its session live then absent, its @opener, a
taken name, a cwd it cannot enter."""

from new.tests import hermetic, live_tmux

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_pm.driver import api, tmux_host

MANAGER = "mgr"
# The child: writes its blocked signals to argv[1], unblocks them, then runs until argv[2] exists, at most 30 s.
CHILD = """import json, os, signal, sys, time
report, go = sys.argv[1:]
with open(report + ".tmp", "w") as f: json.dump(sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK, [])), f)
os.replace(report + ".tmp", report)
signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGHUP, signal.SIGTERM, signal.SIGINT])
for _ in range(600):
    if os.path.exists(go): break
    time.sleep(0.05)
"""


class DetachTest(unittest.TestCase):
    def setUp(self):
        home = os.path.dirname(hermetic.home(self))
        self.server = live_tmux.Server(self, home)
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
        return api.detach(name, self.argv(name), self.dir, self.server.env(), **kwargs)

    def blocked(self, name: str) -> set[int]:
        """The signals child `name` started with blocked, once it reports them."""
        path = os.path.join(self.dir, f"{name}.json")
        live_tmux.wait(lambda: os.path.exists(path), what=f"{name}'s report")
        with open(path) as f:
            return set(json.load(f))

    def gone(self) -> bool:
        # tmux on Linux (CI: 3.4) can leave an exited pane process unreaped until another child of the server exits
        self.server.tmux("run-shell", "-b", "true")
        return api.sessions("d").names == ()

    def test_the_child_runs_with_the_signals_blocked_its_session_live_until_it_exits(self):
        self.assertIsNone(self.detach("d1", opener=MANAGER))
        self.assertEqual(api.sessions("d").state("d1"), "live")
        tmux_host.detach("d0", self.argv("d0"), cwd=self.dir, env=self.server.env())
        signals = {int(s) for s in api.SIGNALS}
        self.assertLessEqual(signals, self.blocked("d1"))
        self.assertEqual(signals & self.blocked("d0"), set())
        self.assertEqual(self.server.option("d1", "@opener"), MANAGER)
        with self.assertRaises(api.DriverError) as caught:
            api.attach("d1")
        self.assertTrue(str(caught.exception).startswith("no tui session d1: "), caught.exception)
        with open(self.go, "w"):
            pass
        live_tmux.wait(self.gone, what="d0 and d1 absent once their children exit")

    def test_a_taken_name_is_refused_and_its_session_left_alone(self):
        self.detach("d1")
        self.blocked("d1")
        pid = self.server.tmux("display-message", "-p", "-t", "=d1:", "#{pane_pid}").stdout
        for name in ("d1", MANAGER):
            with self.subTest(name=name), self.assertRaises(api.DriverError) as caught:
                self.detach(name, opener="other")
            self.assertEqual(str(caught.exception), f"detach {name}: session {name} exists")
        self.assertEqual(
            self.server.tmux("display-message", "-p", "-t", "=d1:", "#{pane_pid}").stdout, pid
        )
        self.assertEqual([self.server.option(n, "@opener") for n in ("d1", MANAGER)], ["", ""])
        self.assertEqual(api.sessions().names, ("d1", MANAGER))

    def test_a_cwd_it_cannot_enter_is_refused_never_a_success(self):
        missing = os.path.join(self.dir, "missing")
        with self.assertRaises(api.ConfigError):
            api.detach("d1", self.argv("d1"), missing, self.server.env())
        with self.assertRaisesRegex(tmux_host.TuiError, "^session d2 did not start$"):
            tmux_host.detach(
                "d2",
                self.argv("d2"),
                cwd=missing,
                env=self.server.env(),
                sleep=lambda seconds: time.sleep(seconds / 10),
            )
        live_tmux.wait(self.gone, what="no session left")


if __name__ == "__main__":
    unittest.main()
