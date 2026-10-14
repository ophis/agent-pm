"""Driver's sessions of the fake runtime and its harness Host's: real child processes."""

import json
import os
import sys
import unittest
from pathlib import Path

from agent_pm.session import Driver
from agent_pm.session.engine.types import Exited

from session.tests.case import HOSTS
from session.tests.fakes.host import FakeHost
from session.tests.hosted_support import (
    WAIT,
    Base,
    TuiBase,
    drive,
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


if __name__ == "__main__":
    unittest.main()
