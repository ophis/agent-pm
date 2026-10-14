"""session/tests/fakes/host.py: FakeHost passes HostContract; its state dir, a dead name's reuse,
send's errors and the group killed until found empty.
"""

import os
import signal
import unittest
from unittest import mock

from agent_pm.session import DriverError
from agent_pm.session.engine.types import Key, Paste

from session.tests.case import Case
from session.tests.contracts.host import SLEEP, HostContract, program
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

    def test_the_group_is_killed_until_found_empty_its_id_free_for_reuse_after(self):
        terminal = self.host.terminal("w1", program(SLEEP), self.home, {})
        with mock.patch.object(os, "killpg", wraps=os.killpg) as killpg:
            terminal.kill()
            terminal.kill()
            terminal.close()
        self.assertEqual([c.args[1] for c in killpg.call_args_list if c.args[1]], [signal.SIGKILL])


if __name__ == "__main__":
    unittest.main()
