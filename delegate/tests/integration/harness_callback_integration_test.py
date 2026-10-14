"""Persisted callbacks consume real HarnessDelegate events without affecting the agent result."""

import json
import os
import subprocess
from unittest import mock

from agent_pm.delegate import DelegateEvent
from agent_pm.session import Exited, Reply

from delegate.tests.fakes.harness_session import HarnessSession
from delegate.tests.harness import SID, HarnessCase
from delegate.tests.replies import block, mark
from delegate.tests.unit.runs import DONE

CALLBACK = ["writeback", "--issue", "ticket-id"]


class Callbacks(HarnessCase):
    def setUp(self):
        super().setUp()
        HarnessSession.script = (Reply(mark("start", "echoing")), Reply(block(DONE)), Exited(0))

    def records(self):
        with open(os.path.join(self.work, "run.jsonl"), encoding="utf-8") as file:
            return [json.loads(line) for line in file]

    def test_callback_argv_receives_one_line_per_actual_event_and_stays_persisted(self):
        delegate = self.make(callback=CALLBACK)
        with mock.patch(
            "subprocess.run", return_value=subprocess.CompletedProcess(CALLBACK, 0)
        ) as run:
            result = delegate.run()
        self.assertEqual(self.metadata()["callback"], CALLBACK)
        self.assertEqual(run.call_count, 3)
        events = []
        for call in run.call_args_list:
            self.assertEqual(tuple(call.args[0]), tuple(CALLBACK))
            self.assertEqual(call.kwargs["cwd"], self.work)
            self.assertEqual(call.kwargs["timeout"], 60)
            line = call.kwargs["input"]
            self.assertEqual(len(line.splitlines()), 1)
            events.append(DelegateEvent.parse(line))
        self.assertEqual((events[0].kind, events[0].sid), ("begun", SID))
        self.assertEqual(
            (events[1].kind, events[1].name, events[1].text), ("progress", "start", "echoing")
        )
        self.assertEqual(
            (events[-1].kind, events[-1].returncode, events[-1].error, events[-1].limit),
            ("finished", result.returncode, result.error, None),
        )
        self.assertEqual(events[-1].outcome, {**DONE, "questions": [], "url": "", "files": []})

    def test_callback_failure_is_logged_and_later_events_still_arrive(self):
        failures = (
            subprocess.CompletedProcess(CALLBACK, 7),
            OSError("callback unavailable"),
            subprocess.TimeoutExpired(CALLBACK, 60),
        )
        for index, failure in enumerate(failures):
            with self.subTest(failure=failure):
                self.work = os.path.join(self.home, f"callback-failure-{index}")
                delegate = self.make(callback=CALLBACK)
                responses = [
                    failure,
                    subprocess.CompletedProcess(CALLBACK, 0),
                    subprocess.CompletedProcess(CALLBACK, 0),
                ]
                with mock.patch("subprocess.run", side_effect=responses) as run:
                    result = delegate.run()
                self.assertEqual(result.returncode, 0)
                self.assertEqual(run.call_count, 3)
                self.assertEqual(
                    DelegateEvent.parse(run.call_args.kwargs["input"]).kind, "finished"
                )
                errors = [row for row in self.records() if row["kind"] == "callback_error"]
                self.assertEqual(len(errors), 1)
                self.assertTrue(errors[0]["error"])

    def test_no_callback_invokes_no_command(self):
        with mock.patch("subprocess.run") as run:
            result = self.make().run()
        self.assertEqual(result.returncode, 0)
        run.assert_not_called()
