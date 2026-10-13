"""session/tests/fakes/host.py: FakeHost passes HostContract; its own refusals, a dead name's reuse,
send's errors and cleanup of a child an ended program left.
"""

import os
import sys
import time
import unittest
from unittest import mock

from agent_pm.session import DriverError
from agent_pm.session.engine.types import Key, Paste

from session.tests.case import Case
from session.tests.contracts.host import PUT, SLEEP, HostContract, gone, program, read
from session.tests.fakes.host import FakeHost


class FakeHostContract(HostContract, Case):
    def make_host(self):
        return FakeHost("fake")


class FakeHostOnly(Case):
    def setUp(self):
        self.host = FakeHost("fake", {"root": "~/hosts"})
        self.addCleanup(lambda: [self.host.attach(n).close() for n in self.host.list()])

    def test_state_lives_under_the_root_setting_expanded(self):
        self.host.detach("w1", program("pass"), self.home, {}).wait_exit(10)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.home, "hosts", "w1"))),
            ["env.json", "meta.json", "output", "pid", "rc"],
        )

    def test_refuses_a_bad_name_argv_or_cwd(self):
        good = (program(SLEEP), self.home, {})
        for name, argv, cwd in (
            ("a b", *good[:2]),
            ("../w", *good[:2]),
            ("", *good[:2]),
            ("w1", [], self.home),
            ("w1", "python3", self.home),
            ("w1", [sys.executable, 1], self.home),
            ("w1", good[0], os.path.join(self.home, "absent")),
        ):
            for kind in ("detach", "terminal"):
                with self.subTest(kind=kind, name=name, argv=argv, cwd=cwd):
                    with self.assertRaises(ValueError):
                        getattr(self.host, kind)(name, argv, cwd, {})
        self.assertEqual(self.host.list(), [])

    def test_a_dead_name_is_started_again(self):
        first = self.host.detach("w1", program("pass"), self.home, {})
        self.assertTrue(first.wait_exit(10))
        again = self.host.terminal("w1", program(SLEEP), self.home, {})
        self.assertTrue(again.alive())
        self.assertIsInstance(self.host.attach("w1"), type(again))

    def test_send_refuses_an_unknown_key_and_an_ended_terminal(self):
        terminal = self.host.terminal("w1", program(SLEEP), self.home, {})
        with self.assertRaises(DriverError):
            terminal.send(Key("F13"))
        terminal.kill()
        for action in (Paste("x"), Key("Enter")):
            with self.subTest(action=action), self.assertRaises(DriverError):
                terminal.send(action)

    def test_kill_and_close_end_a_child_left_by_an_ended_program(self):
        spawn = PUT + (
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "put(str(child.pid))"
        )
        for name, end in (("w1", "kill"), ("w2", "close")):
            with self.subTest(end=end):
                path = os.path.join(self.home, name)
                ended = self.host.detach(name, program(spawn, path), self.home, {})
                self.assertTrue(ended.wait_exit(10))
                child = int(read(path))
                self.assertFalse(gone(child))
                getattr(ended, end)()
                deadline = time.monotonic() + 5
                while not gone(child) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(gone(child))

    def test_the_group_is_killed_once_its_pid_free_for_reuse_after(self):
        terminal = self.host.terminal("w1", program(SLEEP), self.home, {})
        with mock.patch.object(os, "killpg", wraps=os.killpg) as killpg:
            terminal.kill()
            terminal.kill()
            terminal.close()
        self.assertEqual(killpg.call_count, 1)


if __name__ == "__main__":
    unittest.main()
