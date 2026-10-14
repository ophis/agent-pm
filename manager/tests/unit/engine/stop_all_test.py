"""Unknown orphan listings do not prevent control of known roster entries."""

import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from agent_pm.manager.engine.crew import HERE, Crew
from agent_pm.manager.engine.roster import Roster
from agent_pm.manager.engine.types import Spec

from manager.tests.case import Case
from utils.tests.fakes import ctl


class StopAllTest(Case):
    def setUp(self):
        self.command = self.setup_ctl()
        self.roster = Roster(os.path.join(self.agent_pm, "managers", "mgr"))
        self.crew = Crew(self.roster)

    @contextlib.contextmanager
    def unavailable_listing(self):
        native_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == HERE:
                return subprocess.CompletedProcess(argv, 1, "", "listing unavailable")
            return native_run(argv, **options)

        with mock.patch("subprocess.run", side_effect=run):
            yield

    def test_failed_listing_still_closes_known_records_and_keeps_unknown_work_alive(self):
        self.crew.run(Spec("owned", (self.command, "start", "owned")))
        ctl.set_session(self.ctl_dir, "unknown", alive=True)
        stderr = io.StringIO()
        with self.unavailable_listing(), contextlib.redirect_stderr(stderr):
            self.assertEqual(self.crew.stop_all(), ["owned"])
        self.assertIn("listing unavailable", stderr.getvalue())
        self.assertEqual(self.roster.entries(), {})
        with Path(self.ctl_dir, ctl.SESSIONS).open(encoding="utf-8") as stream:
            sessions = json.load(stream)
        self.assertFalse(sessions["owned"]["alive"])
        self.assertTrue(sessions["unknown"]["alive"])
        self.assertEqual(
            [call for call in ctl.calls(self.ctl_dir) if call["argv"] == ["close"]],
            [{"argv": ["close"], "names": ["owned"]}],
        )

    def test_failed_listing_dry_run_preserves_known_records_and_closes_nothing(self):
        self.crew.run(Spec("owned", (self.command, "start", "owned")))
        before = Path(self.roster.path).read_bytes()
        stderr = io.StringIO()
        with self.unavailable_listing(), contextlib.redirect_stderr(stderr):
            self.assertEqual(self.crew.stop_all(dry_run=True), ["owned"])
        self.assertIn("listing unavailable", stderr.getvalue())
        self.assertEqual(Path(self.roster.path).read_bytes(), before)
        self.assertEqual(
            [call for call in ctl.calls(self.ctl_dir) if call["argv"] == ["close"]], []
        )
        with Path(self.ctl_dir, ctl.SESSIONS).open(encoding="utf-8") as stream:
            self.assertTrue(json.load(stream)["owned"]["alive"])

    def test_failed_listing_empty_roster_reports_failure_without_creating_state(self):
        stderr = io.StringIO()
        with self.unavailable_listing(), contextlib.redirect_stderr(stderr):
            self.assertEqual(self.crew.stop_all(), [])
            self.assertEqual(self.crew.stop_all(dry_run=True), [])
        self.assertEqual(stderr.getvalue().count("listing unavailable"), 2)
        self.assertFalse(Path(self.roster.directory).exists())
        self.assertEqual(ctl.calls(self.ctl_dir), [])
