"""core/src/agent_pm/utils/text.py: printable (main's Sinks case)."""

import unittest

from agent_pm.utils.text import printable

from tests.case import Case


class Printable(Case):
    def test_drops_control_characters_but_tabs(self):
        self.assertEqual(printable("a\x1b]52;c;ZXZpbA==\x07b\tc\r\n"), "a]52;c;ZXZpbA==b\tc")


if __name__ == "__main__":
    unittest.main()
