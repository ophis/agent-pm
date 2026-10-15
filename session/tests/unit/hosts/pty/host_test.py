"""session/src/agent_pm/session/hosts/pty/host.py: PtyHost's settings and what it is not."""

import unittest

from agent_pm.session import ConfigError, Displayable
from agent_pm.session.hosts.pty import PtyHost


class Settings(unittest.TestCase):
    def test_outer_terminal_is_its_only_key_a_list_of_variable_names(self):
        self.assertEqual(
            PtyHost("pty", {"outer_terminal": ["A", "B"]}).outer_terminal, frozenset({"A", "B"})
        )
        self.assertEqual(PtyHost("pty").outer_terminal, frozenset())
        for settings in ({"other": 1}, {"outer_terminal": "A"}, {"outer_terminal": ["A", ""]}):
            with self.subTest(settings=settings), self.assertRaises(ConfigError):
                PtyHost("pty", settings)

    def test_it_cannot_display_and_keeps_a_display_as_given(self):
        host = PtyHost("pty")
        self.assertNotIsInstance(host, Displayable)
        self.assertEqual((host.display("x:1.2"), host.display_help()), ("x:1.2", ""))


if __name__ == "__main__":
    unittest.main()
