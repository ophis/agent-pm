"""tests/fakes/host.py: FakeHost passes HostContract; its own refusals, a dead name's reuse and send's errors."""

import os
import sys
import unittest
from unittest import mock

from agent_pm.session import DriverError
from agent_pm.session.engine.api import Key, Paste

from tests.case import Case
from tests.contracts.host import SLEEP, HostContract, program
from tests.fakes.host import FakeHost


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

    def test_kill_after_the_process_ended_signals_nothing(self):
        ended = self.host.detach("w1", program("pass"), self.home, {})
        self.assertTrue(ended.wait_exit(10))
        with mock.patch.object(os, "killpg") as killpg:
            ended.kill()
        killpg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
