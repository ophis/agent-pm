"""Persisted callbacks consume real HarnessDelegate events, each delivered to a real recorder child
(delegate/tests/fakes/callback_recorder.py) from the State dir, without affecting the agent
result.
"""

import json
import os
import sys
from unittest import mock

from agent_pm.delegate import DelegateEvent
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.delegate.engine.state import RunState
from agent_pm.session import Exited, Reply

from delegate.tests.fakes import callback_recorder
from delegate.tests.fakes.harness_session import HarnessSession
from delegate.tests.harness import SID, HarnessCase
from delegate.tests.replies import block, mark
from delegate.tests.unit.runs import DONE

PROGRESS = "回显 one"
OUTCOME = {**DONE, "deliverable": "# 文档\n二\n"}


class Callbacks(HarnessCase):
    def setUp(self):
        super().setUp()
        HarnessSession.script = (Reply(mark("start", PROGRESS)), Reply(block(OUTCOME)), Exited(0))
        self.log = os.path.join(self.home, "callback.log")

    def callback(self, *rules: str, log=None) -> list[str]:
        return [sys.executable, "-P", "-S", callback_recorder.__file__, log or self.log, *rules]

    def delivered(self, log=None) -> list[dict]:
        try:
            with open(log or self.log, encoding="utf-8") as file:
                return [json.loads(line) for line in file]
        except FileNotFoundError:
            return []

    def events(self, log=None) -> list[DelegateEvent]:
        return [DelegateEvent.parse(entry["line"]) for entry in self.delivered(log)]

    def errors(self) -> list[str]:
        with open(os.path.join(self.work, "run.jsonl"), encoding="utf-8") as file:
            rows = [json.loads(line) for line in file]
        return [row["error"] for row in rows if row["kind"] == "callback_error"]

    def test_each_event_is_one_stdin_line_in_order_from_the_state_dir(self):
        delegate = self.make(callback=self.callback())
        result = delegate.run()
        self.assertEqual(self.metadata()["callback"], self.callback())
        entries = self.delivered()
        for entry in entries:
            self.assertEqual(entry["line"].count("\n"), 1)
            self.assertTrue(entry["line"].endswith("\n"))
            self.assertEqual(entry["cwd"], os.path.realpath(RunState(self.work).dir))
            self.assertNotEqual(entry["cwd"], os.path.realpath(self.work))
            self.assertEqual(entry["pgid"], entry["pid"])
        events = self.events()
        self.assertEqual([event.kind for event in events], ["begun", "progress", "finished"])
        self.assertEqual({(event.sid, event.attempt) for event in events}, {(SID, 1)})
        self.assertEqual((events[1].name, events[1].text), ("start", PROGRESS))
        self.assertEqual(
            (events[-1].returncode, events[-1].error, events[-1].limit),
            (result.returncode, result.error, None),
        )
        self.assertEqual(events[-1].outcome, {**OUTCOME, "questions": [], "url": "", "files": []})
        self.assertEqual(self.errors(), [])

    def test_a_failed_delivery_is_one_callback_error_row_and_later_events_still_arrive(self):
        missing = os.path.join(self.home, "missing-callback")
        for index, (rules, command, kinds, errors) in enumerate(
            (
                (("--exit", "begun:1"), None, ["begun", "progress", "finished"], 1),
                ((), [missing], [], 3),
            )
        ):
            with self.subTest(rules=rules, command=command):
                self.work = os.path.join(self.home, f"callback-failure-{index}")
                log = os.path.join(self.home, f"callback-failure-{index}.log")
                result = self.make(callback=command or self.callback(*rules, log=log)).run()
                self.assertEqual(
                    (result.returncode, result.outcome.status if result.outcome else None),
                    (0, "done"),
                )
                self.assertEqual([event.kind for event in self.events(log)], kinds)
                self.assertEqual(len(self.errors()), errors)
                expected = "callback exited 1" if command is None else missing
                self.assertTrue(all(expected in error for error in self.errors()))

    def test_the_harness_side_attach_delivers_to_the_persisted_argv(self):
        self.make(callback=self.callback())
        HarnessDelegate.attach(self.work, session_cls=HarnessSession).run()
        self.assertEqual([event.kind for event in self.events()], ["begun", "progress", "finished"])

    def test_a_replacing_run_stamps_its_own_attempt(self):
        self.make().run()
        replacing = self.make(callback=self.callback())
        replacing.run()
        self.assertEqual({(event.sid, event.attempt) for event in self.events()}, {(SID, 2)})

    def test_no_callback_invokes_no_command(self):
        with mock.patch("subprocess.Popen") as popen:
            result = self.make().run()
        self.assertEqual(result.returncode, 0)
        popen.assert_not_called()
