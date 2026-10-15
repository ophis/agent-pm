"""LinearRun on PtyHost (FR-47 356-16): with `pty` the [hosts] default, router --issue detaches the
run, whose harness runs on a pty (~/.agent-pm/pty/<name>/, nothing on tmux) and writes back to In
Review; agent-pm-linear status, resume and close then answer through LinearRun.attach: no attach
command, and no new run once it has its outcome.
"""

import json
import os
import unittest

from agent_pm.utils import Record
from orchestrator_tests.integration.flow_fixtures import (
    ACCOUNTS,
    ENTRY,
    PROJECT,
    REPO,
    TIMEOUT,
    TmuxFlow,
)
from orchestrator_tests.integration.segments import agent_calls

from delegate.tests.replies import block, mark
from session.tests import live_tmux

ID = "TASK-7"
START = mark("start", "Building the session registry.")
DONE = block(
    {
        "status": "done",
        "title": "Session registry",
        "summary": "Added the session registry.",
        "url": f"https://github.com/{REPO}/pull/1",
    }
)


class PtyRun(TmuxFlow):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.agent_pm, "config.local.toml"), "a", encoding="utf-8") as f:
            f.write('\n[session.hosts]\ndefault = "pty"\n')
        self.fake.issue(
            "ENG: Session registry",
            identifier=ID,
            assignee=self.ids[ACCOUNTS["engineer"]],
            project=PROJECT,
            description="Add a session registry.",
        )

    def on_pty(self, name: str) -> bool:
        return os.path.isdir(os.path.join(self.agent_pm, "pty", name))

    def linear(self, *argv: str):
        res = self.script(ENTRY, *argv)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_a_run_on_the_pty_host_writes_back_and_answers_status_resume_and_close(self):
        gate = os.path.join(self.root, "gate")
        self.scene([START, DONE], gate=gate)
        res = self.router("--issue", ID)
        self.assertEqual(res.returncode, 0, res.stderr)
        sid = self.runs()[-1]["sid"]
        harness = self.harness("engineer", ID, sid)
        live_tmux.wait(lambda: agent_calls(self.calls()), TIMEOUT, "the agent at its gate")
        self.assertTrue(self.on_pty(harness))
        self.assertFalse(self.live(harness))
        (status,) = map(json.loads, self.linear("status", ID).splitlines())
        self.assertEqual((status["alive"], status["attach"]), (True, None))
        with open(gate, "w", encoding="utf-8"):
            pass
        self.wait_end(sid, ID)
        live_tmux.wait(lambda: not self.on_pty(harness), TIMEOUT, f"{harness} gone")
        self.assertEqual(self.state(ID), "in_review")
        (status,) = map(json.loads, self.linear("status", ID).splitlines())
        self.assertEqual(
            (status["alive"], status["attach"], status["outcome"]["status"]), (False, None, "done")
        )
        starts = len(agent_calls(self.calls()))
        record = Record.parse(self.linear("resume", ID))
        self.assertEqual(
            (record.sid, record.sessions, record.ctl), (sid, (harness,), ("agent-pm-linear",))
        )
        self.assertEqual(len(agent_calls(self.calls())), starts)
        self.linear("close", ID)
        self.assertFalse(self.on_pty(harness))


if __name__ == "__main__":
    unittest.main()
