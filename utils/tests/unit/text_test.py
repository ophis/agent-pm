"""utils/src/agent_pm/utils/text.py: printable (main's Sinks case)."""

import unittest

from agent_pm.utils import sid_error
from agent_pm.utils.text import printable

from utils.tests.case import Case


class Printable(Case):
    def test_drops_control_characters_but_tabs(self):
        self.assertEqual(printable("a\x1b]52;c;ZXZpbA==\x07b\tc\r\n"), "a]52;c;ZXZpbA==b\tc")


class SidError(Case):
    def test_accepts_nonempty_printable_identifiers_without_a_slash(self):
        for sid in ("session-1", "thread_2", "space allowed", "é", "a" * 200):
            with self.subTest(sid=sid):
                self.assertIsNone(sid_error(sid))

    def test_rejects_invalid_identifiers_with_the_original_error(self):
        for sid in (None, 1, b"sid", "", "a/b", "a\tb", "a\nb", "a\0b"):
            with self.subTest(sid=sid):
                self.assertEqual(
                    sid_error(sid),
                    f'sid must be a non-empty printable string without "/", got {sid!r:.80}',
                )

    def test_limits_the_identifier_in_the_error(self):
        sid = "a" * 200 + "\n"
        self.assertEqual(
            sid_error(sid),
            'sid must be a non-empty printable string without "/", got ' + "'" + "a" * 79,
        )


if __name__ == "__main__":
    unittest.main()
