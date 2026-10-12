"""session/engine/status_log.py: SessionStatusLog.line's format, clock and refusals; write's appends and refusals; a Roster
rotation between writes loses no line."""

import os
import stat
import time
import unittest
from unittest import mock

from agent_pm.manager import roster
from agent_pm.session import SessionStatusLog

from tests.case import Case

AT = "12:00:00"


class Line(Case):
    def test_a_fixed_at_joins_name_word_and_fields(self):
        self.assertEqual(SessionStatusLog.line("w1", "dead", at=AT), "12:00:00 w1 dead")
        self.assertEqual(
            SessionStatusLog.line("w1", "exited", 7, "two words", at=AT),
            "12:00:00 w1 exited 7 two words",
        )

    def test_without_at_the_clock_is_read_once(self):
        with mock.patch.object(time, "strftime", return_value="12:34:56") as clock:
            self.assertEqual(SessionStatusLog.line("w1", "done"), "12:34:56 w1 done")
        clock.assert_called_once_with(SessionStatusLog.CLOCK)
        self.assertEqual(SessionStatusLog.CLOCK, "%H:%M:%S")

    def test_refuses_a_bad_name_word_field_or_at(self):
        cases = [
            ("", "done", (), AT),
            ("w 1", "done", (), AT),
            ("w1\t", "done", (), AT),
            (None, "done", (), AT),
            ("w1", "", (), AT),
            ("w1", " done", (), AT),
            ("w1", "do\nne", (), AT),
            ("w1", "done", ("a\nb",), AT),
            ("w1", "done", ("ok", "\x1b[2J"), AT),
            ("w1", "done", (), "12:00\n"),
        ]
        for name, word, fields, at in cases:
            with self.subTest(name=name, word=word, fields=fields, at=at):
                with self.assertRaises(ValueError):
                    SessionStatusLog.line(name, word, *fields, at=at)

    def test_refuses_an_empty_field_naming_it(self):
        for fields in (("",), ("ok", "")):
            with self.subTest(fields=fields), self.assertRaises(ValueError) as caught:
                SessionStatusLog.line("w1", "done", *fields, at=AT)
            self.assertIn("event field:", str(caught.exception))


class Write(Case):
    def setUp(self):
        self.path = os.path.join(self.home, "events")
        self.events = SessionStatusLog(self.path)

    def read(self, path=None):
        with open(path or self.path) as f:
            return f.read()

    def test_appends_one_line_per_write_to_a_new_0600_file(self):
        self.addCleanup(os.umask, os.umask(0o022))
        with mock.patch.object(time, "strftime", return_value=AT):
            self.events.write("w1", "done")
            self.events.write("w1", "exited", 0)
        self.assertEqual(self.read(), "12:00:00 w1 done\n12:00:00 w1 exited 0\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_refuses_a_symlink_and_writes_no_bad_line(self):
        target = os.path.join(self.home, "target")
        open(target, "w").close()
        os.symlink(target, self.path)
        with self.assertRaises(OSError):
            self.events.write("w1", "done")
        self.assertEqual(self.read(target), "")
        os.unlink(self.path)
        with self.assertRaises(ValueError):
            self.events.write("w 1", "done")
        self.assertFalse(os.path.lexists(self.path))


class Rotation(Case):
    def test_a_roster_rotation_between_writes_loses_no_line(self):
        directory = os.path.join(self.agent_pm, "managers", "m1")
        roster.ensure(directory)
        events = SessionStatusLog(roster.events(directory))
        lines = [
            f"{SessionStatusLog.line('w1', 'done', f'{i:08d}', at=AT)}\n"
            for i in range(roster.EVENTS_MAX // 26 + 4)
        ]
        with open(events.path, "a") as f:
            f.write("".join(lines))
        self.assertGreater(os.path.getsize(events.path), roster.EVENTS_MAX)
        r = {"version": 1, "holder": None, "cursor": len(lines) - 3, "gen": 0, "entries": {}}
        with mock.patch.object(time, "strftime", return_value=AT):
            rotated = roster.rotate(r, directory, sleep=lambda seconds: events.write("w2", "done"))
            events.write("w3", "exited", 0)
        self.assertIs(rotated, True)
        self.assertEqual((r["cursor"], r["gen"]), (0, 1))
        with open(events.path) as f:
            self.assertEqual(
                f.read(), "12:00:00 w2 done\n" + "".join(lines[-3:]) + "12:00:00 w3 exited 0\n"
            )
        with open(events.path + ".1") as f:
            self.assertEqual(f.read(), "".join(lines))


if __name__ == "__main__":
    unittest.main()
