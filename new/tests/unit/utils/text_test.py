"""new/core/src/utils/text.py: printable (main's Sinks case)."""

from new.tests import SRC  # noqa: F401

import unittest

from utils.text import printable


class Printable(unittest.TestCase):
    def test_drops_control_characters_but_tabs(self):
        self.assertEqual(printable("a\x1b]52;c;ZXZpbA==\x07b\tc\r\n"), "a]52;c;ZXZpbA==b\tc")


if __name__ == "__main__":
    unittest.main()
