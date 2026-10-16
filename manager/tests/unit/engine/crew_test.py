"""Crew.run: the starter command's final Record becomes the live entry; a failed start is lost."""

import contextlib
import io
import os
import uuid
from dataclasses import replace
from unittest import mock

from agent_pm.manager.engine.crew import Crew
from agent_pm.manager.engine.types import Spec, State
from agent_pm.utils import Status

from manager.tests.roster_case import RosterCase
from utils.tests.process_fixtures import DEVNULL, TimeoutExpired

ARGV = ("fake-ctl", "start", "x")


class Run(RosterCase):
    def start(self, run, name="x"):
        stderr = io.StringIO()
        with (
            mock.patch("subprocess.run", side_effect=run) as called,
            contextlib.redirect_stderr(stderr),
        ):
            try:
                return Crew(self.roster).run(Spec(name, ARGV)), called, stderr.getvalue()
            except RuntimeError as error:
                return error, called, stderr.getvalue()

    def test_the_final_record_on_stdout_becomes_the_live_entry(self):
        def run(_argv, **options):
            record = replace(self.record, sid=options["env"]["AGENT_PM_SID"])
            os.write(options["stdout"].fileno(), f"progress\n{record.to_line()}\n".encode())
            return mock.Mock(returncode=0, stderr="warning\n")

        record, called, stderr = self.start(run)
        entry = self.roster.entries()["x"]
        self.assertEqual((entry.state, entry.record), (State.LIVE, record))
        self.assertEqual(record, replace(self.record, sid=entry.sid))
        self.assertEqual(called.call_args.args, (ARGV,))
        env = called.call_args.kwargs["env"]
        self.assertEqual(env["AGENT_PM_EVENTS"], self.roster.events)
        self.assertEqual(env["AGENT_PM_SID"], entry.sid)
        self.assertIs(called.call_args.kwargs["stdin"], DEVNULL)
        self.assertEqual(stderr, "warning\n")

    def test_a_failed_start_raises_and_marks_the_entry_lost(self):
        for name, effect, message in (
            ("stderr", mock.Mock(returncode=2, stderr=" boom \n"), "boom"),
            ("exit", mock.Mock(returncode=2, stderr=""), "exit 2"),
            ("missing", FileNotFoundError(2, "No such file"), "[Errno 2] No such file"),
            ("timeout", TimeoutExpired(ARGV, 30), "no response within 30 seconds"),
        ):
            with self.subTest(name=name):
                error, _, _ = self.start(mock.Mock(side_effect=[effect]), name)
                self.assertEqual(str(error), f"fake-ctl start x: {message}")
                self.assertIs(self.roster.entries()[name].state, State.LOST)


class RecordlessStatus(RosterCase):
    def test_a_launch_without_a_record_is_starting_then_gone_once_lost(self):
        starting = self.roster.begin(Spec("s", ARGV), sid=str(uuid.uuid4()))
        lost = self.roster.begin(Spec("l", ARGV), sid=str(uuid.uuid4()))
        self.assertTrue(self.roster.mark_if("l", lost, State.LOST))
        self.assertIsNone(starting.record)
        self.assertEqual(
            Crew(self.roster).status(),
            [
                Status(
                    name="l", alive=False, state="gone", outcome=None, attach=None, hidden=False
                ),
                Status(
                    name="s", alive=False, state="starting", outcome=None, attach=None, hidden=False
                ),
            ],
        )
