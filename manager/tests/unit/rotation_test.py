"""A Roster rotation between SessionStatusLog writes loses no line."""

import os
import time
import unittest
from unittest import mock

from agent_pm.manager import roster
from agent_pm.session import SessionStatusLog

from manager.tests.case import Case

AT = "12:00:00"


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
