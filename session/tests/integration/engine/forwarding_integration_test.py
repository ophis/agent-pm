"""Driver's sessions of the fake client and its harness Host's: real child processes."""

import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agent_pm.session import Driver
from agent_pm.session.engine.types import Exited, Mode

from session.tests.case import HOSTS
from session.tests.fakes.host import FakeHost
from session.tests.hosted_support import (
    LAUNCHED,
    SID,
    WAIT,
    Base,
    TuiBase,
    drive,
    fake_tmux,
    listed,
)

FAKE_HOSTS = {"fake": {"implementation": "session.tests.fakes.host:FakeHost"}}


class StartTest(Base):
    def test_the_spec_sid_is_the_sessions_and_may_be_resumes(self):
        for kwargs in ({}, {"resume": "s1"}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(drive(self, spec=self.spec(sid="s1"), **kwargs).sid, "s1")

    def test_resume_continues_that_session_and_sid_may_repeat_it(self):
        for kwargs in ({"resume": "r1"}, {"sid": "r1", "resume": "r1"}):
            with self.subTest(kwargs=kwargs):
                session = drive(self, **kwargs)
                self.assertEqual(session.sid, "r1")
                self.assertEqual(self.drain(session), [Exited(0)])
                with open(self.log, encoding="utf-8") as f:
                    argv = json.loads(f.readlines()[-1])["argv"]
                self.assertIn("--resume", argv)


class DetachTest(TuiBase):
    def test_the_harness_host_runs_it_with_the_opener_as_its_display(self):
        driver = Driver(
            adapters={},
            hosts={**HOSTS, **FAKE_HOSTS},
            default_host="tmux",
            host=self.host,
            harness_host="fake",
        )
        gate = os.path.join(self.dir, "finish")
        argv = [
            sys.executable,
            "-I",
            "-c",
            "import os, sys, time\nwhile not os.path.exists(sys.argv[1]): time.sleep(0.01)",
            gate,
        ]
        self.assertIsNone(driver.detach("d1", argv, self.dir, {"PATH": "/bin"}, opener="mgr"))
        process = driver.session_cls.host("fake").attach("d1")
        self.addCleanup(process.close)
        self.assertIsInstance(driver.session_cls.host("fake"), FakeHost)
        self.assertEqual((process.display(), process.record()), ("mgr", None))
        Path(gate).touch()
        self.assertTrue(process.wait_exit(WAIT))
        self.assertEqual(self.host.mock_calls, [])


class WrappersTest(TuiBase):
    def test_start_and_attach_give_sessions_of_the_session_class(self):
        session = drive(self, mode="tui", name="w1", sid=SID)
        self.assertIsInstance(session, self.driver.session_cls)
        self.assertIsInstance(self.driver.attach("w1"), self.driver.session_cls)


class SpecMappingTest(TuiBase):
    """Driver.start puts its arguments on the one Spec its Runner and Adapter read (resume=X: sid=X,
    resume=True); idle_timeout goes beside the Spec.
    """

    def setUp(self):
        super().setUp()
        LAUNCHED.clear()

    def test_client_mode_prompt_resume_and_events_reach_the_adapters_spec_idle_timeout_not(self):
        session = drive(
            self,
            client="inner",
            mode="tui",
            name="w1",
            prompt="hi",
            resume=SID,
            events=self.events,
            idle_timeout=5,
        )
        drive(self, client="inner", mode="tui", name="w2", sid=SID)
        first, second = LAUNCHED
        self.assertEqual(
            (first.client, first.mode, first.prompt, first.sid, first.resume, first.events),
            ("inner", Mode.TUI, "hi", SID, True, self.events),
        )
        self.assertFalse(hasattr(first, "idle_timeout"))
        self.assertEqual((session.client, session.mode, session.sid), ("inner", "tui", SID))
        self.assertEqual(
            (second.prompt, second.sid, second.resume, second.events),
            ("hello", SID, False, None),
        )

    def test_neither_sid_nor_resume_gives_a_new_sid_and_no_resume(self):
        drive(self, client="inner", mode="tui", name="w1")
        (spec,) = LAUNCHED
        self.assertEqual((uuid.UUID(spec.sid).version, spec.resume), (4, False))


class AssignedTest(TuiBase):
    def test_a_host_assigned_starts_lists_and_closes_its_later_sessions(self):
        old, new = self.host, fake_tmux(self)
        before = drive(self, mode="tui", name="w1")
        self.driver.host = new
        drive(self, mode="tui", name="w2")
        new.listing.return_value = [listed("w2")]
        self.assertEqual([row.name for row in self.driver.sessions()], ["w2"])
        self.driver.close("w2")
        self.assertEqual(
            [[c.args[0] for c in tmux.start.call_args_list] for tmux in (old, new)],
            [["w1"], ["w2"]],
        )
        self.assertEqual(new.kill.call_args_list, [mock.call("%1")])
        self.assertEqual(self.driver.attach_command("w2"), "tmux attach -t '=w2'")
        new.status.reset_mock()
        before.state()
        new.status.assert_not_called()
        old.status.assert_called_with("%1")


class CloseTest(TuiBase):
    def test_close_and_attach_then_close_leave_the_same_state_fr_4(self):
        """FR-4: Session.attach(n).close() and close(n) each kill tmux session n alone and remove
        its session dir.
        """
        found = {}
        for name, close_it in (
            ("w1", lambda: self.session.attach("w1").close()),
            ("w2", lambda: self.driver.close("w2")),
        ):
            with self.subTest(name=name):
                self.tui(name=name, sid=SID)
                self.host.reset_mock()
                close_it()
                found[name] = [c for c in self.host.mock_calls if c[0] == "kill"]
                self.assertFalse(os.path.lexists(os.path.join(self.root, name)))
                self.assertTrue(os.path.isdir(self.root))
        self.assertEqual(found, {"w1": [mock.call.kill("%1")], "w2": [mock.call.kill("%1")]})


if __name__ == "__main__":
    unittest.main()
