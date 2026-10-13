"""Crew control permissions and lock boundaries through executable fake commands."""

import os
import subprocess
from pathlib import Path

from agent_pm.manager.engine.crew import Crew
from agent_pm.manager.engine.roster import Roster
from agent_pm.manager.engine.types import Held, Spec
from agent_pm.utils import Status

from manager.tests.case import Case
from utils.tests.fakes import ctl


class Control(Case):
    def setUp(self):
        self.command = self.setup_ctl()
        self.directory = os.path.join(self.home, "roster")
        self.roster = Roster(self.directory)
        crew = Crew(self.roster)
        self.records = [
            crew.run(Spec(name, (self.command, "start", name))) for name in ("first", "second")
        ]

    def assert_unchanged_and_alive(self, entries, ledger):
        self.assertEqual(self.roster.entries(), entries)
        self.assertEqual(Path(self.roster.path).read_bytes(), ledger)
        result = subprocess.run(
            (self.command, "status"),
            input="".join(record.to_line() + "\n" for record in self.records),
            capture_output=True,
            text=True,
            check=True,
        )
        statuses = [Status.parse(line) for line in result.stdout.splitlines()]
        self.assertEqual([status.name for status in statuses], ["first", "second"])
        self.assertTrue(all(status.alive for status in statuses))
        self.assertEqual(
            [call for call in ctl.calls(self.ctl_dir) if call["argv"][0] == "close"], []
        )

    def test_non_holder_stop_closes_nothing(self):
        with self.roster.hold("owner"):
            entries = self.roster.entries()
            ledger = Path(self.roster.path).read_bytes()
            other = Crew(Roster(self.directory))
            with self.assertRaises(Held):
                other.stop("first")
            self.assert_unchanged_and_alive(entries, ledger)

    def test_non_holder_stop_all_closes_nothing(self):
        with self.roster.hold("owner"):
            entries = self.roster.entries()
            ledger = Path(self.roster.path).read_bytes()
            other = Crew(Roster(self.directory))
            with self.assertRaises(Held):
                other.stop_all()
            self.assert_unchanged_and_alive(entries, ledger)
