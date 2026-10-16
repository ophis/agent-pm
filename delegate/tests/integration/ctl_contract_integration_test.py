"""The delegate ctl contract through fresh CLI processes and the persistent process-free Host."""

import json
import os
import subprocess
from pathlib import Path

from agent_pm.delegate import Outcome
from agent_pm.delegate.engine.harness_delegate import HarnessDelegate
from agent_pm.delegate.engine.types import Result
from agent_pm.utils import SID_ENV, Record

from delegate.tests.case import PY, REPO, child_env
from delegate.tests.fakes.harness_session import DiskHost
from delegate.tests.harness import HarnessCase
from utils.tests.contracts.ctl import AnswerCtlContract


class DelegateCtlContract(AnswerCtlContract, HarnessCase):
    command = "agent-pm-delegate"

    def setUp(self):
        super().setUp()
        with open(self.local, encoding="utf-8") as file:
            settings = file.read()
        scenario = os.path.join(self.home, "scenario.json")
        Path(scenario).write_text("[]", encoding="utf-8")
        settings = settings.replace(
            'implementation = "session.tests.fake_client:FakeClient"',
            'implementation = "session.tests.fake_client:FakeClient"\n'
            + f"scenario = {json.dumps(scenario)}\n"
            + f"log = {json.dumps(os.path.join(self.home, 'client.jsonl'))}",
        )
        with open(self.local, "w", encoding="utf-8") as file:
            file.write(settings.replace(":RecordingHost", ":DiskHost"))
        self.started_records = {}

    def call(self, *argv, stdin="", sid=None):
        env = child_env(None, REPO)
        env.pop(SID_ENV, None)
        if sid is not None:
            env[SID_ENV] = sid
        return subprocess.run(
            (*PY, "-m", "agent_pm.delegate.cli", *argv),
            cwd=self.home,
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def start(self, name, sid=None):
        workdir = os.path.join(self.home, name)
        done = self.call(
            "run",
            "--role",
            "dummy-tester",
            "--client",
            "fake",
            "--input",
            "initial",
            "--workdir",
            workdir,
            "--detach",
            "--mode",
            "headless",
            "--events",
            os.path.join(self.home, "events"),
            sid=sid,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(done.stdout.splitlines()), 1, done.stdout)
        record = Record.parse(done.stdout)
        self.started_records[name] = record
        return record

    def inventory(self):
        root = DiskHost("agent").root()
        if not os.path.isdir(root):
            return {}
        return {name: Path(root, name).read_text(encoding="utf-8") for name in os.listdir(root)}

    def refuse(self):
        return self.call(
            "run",
            "--role",
            "no-such-role",
            "--input",
            "initial",
            "--detach",
            "--mode",
            "headless",
            "--events",
            os.path.join(self.home, "events"),
        )

    def end(self, name):
        for session in self.started_records[name].sessions:
            DiskHost("agent").attach(session).close()

    def waiting(self, record):
        view = HarnessDelegate.attach(record.workdir)
        view._complete(
            Result(0, Outcome("needs_input", "Question", "Waiting", questions=["Answer?"]))
        )
        view.close()

    def input_of(self, record):
        view = HarnessDelegate.attach(record.workdir)
        return view._read().plan.spec.input
