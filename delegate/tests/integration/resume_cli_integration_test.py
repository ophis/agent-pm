"""agent-pm-delegate resume, each a fresh process, on a detached headless run whose Hosts keep their
sessions on disk (delegate/tests/fakes/harness_session.py PersistentSession): one Record line, a
live run's unchanged, a stopped run's resumed once (two concurrent commands included), and a run
of another $AGENT_PM_SID refused.
"""

import json
import os
import subprocess
import uuid

from agent_pm.delegate import Spec
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.utils import Record

from delegate.tests.case import PY, REPO, child_env
from delegate.tests.fakes.harness_session import PersistentSession
from delegate.tests.harness import SID, HarnessCase

RESUME = (*PY, "-m", "agent_pm.delegate.cli", "resume")


class ResumeCommand(HarnessCase):
    def setUp(self):
        super().setUp()
        self.record = HarnessDelegate(
            "dummy-tester",
            Spec("input", self.work, client="fake", sid=SID),
            session_cls=PersistentSession,
            host="agent",
            harness_host="harness",
            display="opaque",
        ).detach()

    def env(self, sid):
        env = child_env(None, REPO)
        env.pop("AGENT_PM_SID", None)
        return env if sid is None else {**env, "AGENT_PM_SID": sid}

    def resume(self, *keys, stdin="", sid=None):
        return subprocess.run(
            (*RESUME, *keys),
            cwd=self.home,
            env=self.env(sid),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def resumed(self, *keys, stdin="", sid=None) -> Record:
        res = self.resume(*keys, stdin=stdin, sid=sid)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(res.stdout.splitlines()), 1, res.stdout)
        return Record.parse(res.stdout)

    def launches(self):
        path = os.path.join(self.agent_pm, "harness-fake", "launches.jsonl")
        with open(path, encoding="utf-8") as file:
            return [(launch["host"], launch["name"]) for launch in map(json.loads, file)]

    def end(self):
        PersistentSession.host("harness").attach(self.record.sessions[0]).close()

    def test_a_live_run_prints_its_record_from_the_workdir_or_stdin_starting_nothing(self):
        self.assertEqual(self.resumed(self.work), self.record)
        self.assertEqual(self.resumed(stdin=self.record.to_line() + "\n"), self.record)
        self.assertEqual(self.launches(), [("harness", self.record.sessions[0])])
        self.assertEqual(self.metadata()["attempt"], 1)

    def test_a_stopped_run_resumes_once_keeping_its_identity_and_placement(self):
        self.end()
        self.assertEqual(self.resumed(self.work, sid=SID), self.record)
        self.assertEqual(self.record.display, "opaque")
        self.assertEqual(self.launches(), [("harness", self.record.sessions[0])] * 2)
        self.assertEqual(self.metadata()["attempt"], 2)
        self.assertEqual(self.resumed(self.work, sid=SID), self.record)
        self.assertEqual(len(self.launches()), 2)
        self.assertEqual(self.metadata()["attempt"], 2)

    def test_another_agent_pm_sid_exits_2_starting_nothing(self):
        other = str(uuid.uuid4())
        refused = (2, "", f"delegate: {self.work}: sid {other} is not the run's (its sid: {SID})\n")
        for ended in (False, True):
            if ended:
                self.end()
            with self.subTest(ended=ended):
                res = self.resume(self.work, sid=other)
                self.assertEqual((res.returncode, res.stdout, res.stderr), refused)
                self.assertEqual(len(self.launches()), 1)
                self.assertEqual(self.metadata()["attempt"], 1)
        self.assertEqual(self.resumed(self.work, sid=SID), self.record)
        self.assertEqual(len(self.launches()), 2)

    def test_two_resumes_started_together_launch_one_harness(self):
        self.end()
        children = [
            subprocess.Popen(
                (*RESUME, self.work),
                cwd=self.home,
                env=self.env(SID),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        for child in children:
            self.addCleanup(_close, child)
        records = []
        for child in children:
            stdout, stderr = child.communicate(timeout=60)
            self.assertEqual(child.returncode, 0, stderr)
            records.append(Record.parse(stdout))
        self.assertEqual(records, [self.record] * 2)
        self.assertEqual(len(self.launches()), 2)
        self.assertEqual(self.metadata()["attempt"], 2)


def _close(child):
    if child.poll() is None:
        child.kill()
    child.communicate(timeout=10)
