"""Printable text, exception messages and ISO timestamps."""

import re
import unittest
from datetime import UTC, datetime, timedelta, timezone

from agent_pm.utils import one_line, parse_time, sid_error, timestamp
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


class OneLine(Case):
    def test_strings_collapse_whitespace_and_other_values_keep_their_text(self):
        for value, expected in (
            ("a\n\n  b\tc ", "a b c"),
            (" \t\n", ""),
            (42, "42"),
            (None, "None"),
        ):
            with self.subTest(value=value):
                self.assertEqual(one_line(value), expected)

    def test_exception_type_and_message_exclude_other_attributes(self):
        error = RuntimeError("two\nlines")
        error.credential = "private attribute"
        self.assertEqual(one_line(error), "RuntimeError: two lines")


class ParseTime(Case):
    def test_utc_and_explicit_offsets_preserve_the_instant(self):
        expected = datetime(2026, 10, 14, 12, 34, 56, tzinfo=UTC)
        for value in (
            "2026-10-14T12:34:56Z",
            "2026-10-14T12:34:56+00:00",
            "2026-10-14T08:34:56-04:00",
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_time(value), expected)

    def test_fractional_seconds_and_naive_values_are_preserved(self):
        self.assertEqual(
            parse_time("2026-10-14T12:34:56.123456Z"),
            datetime(2026, 10, 14, 12, 34, 56, 123456, tzinfo=UTC),
        )
        self.assertEqual(parse_time("2026-10-14T12:34:56"), datetime(2026, 10, 14, 12, 34, 56))

    def test_invalid_timestamps_raise_value_error(self):
        for value in ("", "not a time", "2026-13-14T12:34:56Z"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_time(value)


class Timestamp(Case):
    def test_default_timestamp_is_local_time_in_seconds(self):
        self.assertRegex(timestamp(), r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d\Z")

    def test_local_timestamp_has_seconds_precision_and_preserves_the_instant(self):
        instant = datetime(2026, 10, 14, 8, 34, 56, 987654, tzinfo=timezone(timedelta(hours=-4)))
        value = timestamp(now=lambda: instant)
        self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", value))
        self.assertEqual(parse_time(value), instant.replace(microsecond=0))


if __name__ == "__main__":
    unittest.main()
