"""FR-25 and FR-29 end to end: bin/agent-pm-linear router by absolute path, a launchd-like PATH and
no PYTHONPATH. The router claims, writes the start row and exits while the run's harness and agent
go on; the harness finds `agent-pm-linear` in this installation's bin/ and calls it back on each
event, so the issue gets its session comment, progress and outcome once each. Its python3 is
fake_python.py: this interpreter under the fake Linear transport with the test roots its fakes
import, the only command added to PATH.
"""

import os
import shlex
import subprocess
import sys
import unittest

from orchestrator_tests.case import REPO
from orchestrator_tests.integration.flow_fixtures import (
    ACCOUNTS,
    HARNESS_EMAIL,
    PROJECT,
    TIMEOUT,
    TmuxFlow,
)
from orchestrator_tests.integration.segments import agent_calls

from delegate.tests.replies import block, mark
from session.tests import live_tmux

ID = "TASK-7"
BIN = os.path.join(REPO, "bin", "agent-pm-linear")
TESTS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAKE_PYTHON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_python.py")
STARTED, READY = "Building the session registry.", "Added the session registry."
PR = "https://github.com/ophis/agent-pm/pull/1"
DONE = block({"status": "done", "title": "Session registry", "summary": READY, "url": PR})


class MinimalPath(TmuxFlow):
    def setUp(self):
        super().setUp()
        os.unlink(os.path.join(self.bin, "agent-pm-linear"))  # the callback is the installed one
        self.python = os.path.join(self.root, "python")
        os.mkdir(self.python)
        with open(shim := os.path.join(self.python, "python3"), "w") as f:
            f.write(
                f'#!/bin/sh\nPYTHONPATH="$PYTHONPATH:{TESTS}:{REPO}" '
                f'exec {shlex.quote(sys.executable)} -P -s {shlex.quote(FAKE_PYTHON)} "$@"\n'
            )
        os.chmod(shim, 0o755)
        self.fake.issue(
            "ENG: Session registry",
            identifier=ID,
            assignee=self.ids[ACCOUNTS["engineer"]],
            project=PROJECT,
            description="Add a session registry.",
        )

    def test_the_router_exits_and_the_harness_calls_this_installations_writeback(self):
        gate = os.path.join(self.root, "gate")
        self.scene([mark("start", STARTED), DONE], gate=gate)
        env = {k: v for k, v in self.env().items() if k != "PYTHONPATH"}
        env.update(PATH=f"{self.python}:/usr/bin:/bin", AGENT_PM_PATH=f"{self.bin}:{self.python}")
        res = subprocess.run(
            [BIN, "router", "--issue", ID],
            cwd=self.root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        self.assertEqual((res.returncode, res.stdout), (0, ""), (res.stderr, self.said()))
        [row] = self.runs()
        sid = row["sid"]
        harness = self.harness("engineer", ID, sid)
        live_tmux.wait(lambda: agent_calls(self.calls()), TIMEOUT, "the agent at its gate")
        self.assertTrue(self.live(harness))
        with open(gate, "w"):
            pass
        self.wait_end(sid, ID)
        self.assertEqual(self.state(ID), "in_review")
        (author, session), *rest = self.comments(ID)
        self.assertEqual(author, HARNESS_EMAIL)
        self.assertRegex(session, rf"^Run {sid} · done · .* · exit 0\n")
        engineer = ACCOUNTS["engineer"]
        self.assertEqual(
            rest, [(engineer, f"Build started: {STARTED}"), (engineer, f"Build ready: {READY}")]
        )
        self.assertEqual(
            self.ledger(ID),
            {sid: ["start", "comment", f"attach:{PR}", "move:in_review", "outcome"]},
        )
        self.assertEqual(len(self.sent("linear.M_COMMENT_ID", HARNESS_EMAIL)), 1)


if __name__ == "__main__":
    unittest.main()
