"""Public CLI runs, --client opencode, over
session/tests/integration/clients/opencode/fake_opencode.py (on PATH as `opencode`) on the default
tmux Host, headless: the Delegate's part of the opencode client.
"""

import json
import unittest

from agent_pm.delegate.engine.runner import FIX
from agent_pm.delegate.engine.types import Layout

from delegate.tests.integration.cli import DONE, UNSAVED, UNSAVED_ERROR, OpenCodeIntegration
from delegate.tests.replies import block, mark
from session.tests.integration.clients.opencode import fake_opencode

NATIVE = fake_opencode.NATIVE


def said(*texts) -> list[dict]:
    return [{"text": text} for text in texts]


class OpenCode(OpenCodeIntegration):
    def test_reports_start_and_valid_outcome(self):
        res = self.drive(said(mark("start", "echoing"), "OpenCode says hello.", block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        for line in ("Progress (start): echoing", "OpenCode says hello.", "delegate: status done"):
            self.assertIn(line, res.stderr)
        self.assertEqual(self.read(self.out), "Hello.\n")
        self.assertEqual(self.texts("client_session", "client_id"), [NATIVE])
        (call,) = self.calls()
        self.assertEqual(call["cwd"], Layout(self.work).agent)
        self.assertEqual(call["argv"][:3], ["run", "--format", "json"])
        self.assertIsNone(call["session"])
        self.assertIn("# Input\n\nHello.", call["prompt"])
        provider = call["config"]["provider"]["ollama"]
        self.assertEqual(provider["options"]["baseURL"], "http://127.0.0.1:11434/v1")
        self.assertFalse(call["auto"])

    def test_an_invalid_outcome_is_fixed_by_one_resume(self):
        res = self.drive(said(mark("start", "working"), block(UNSAVED)), fixes=said(block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"delegate: invalid outcome: {UNSAVED_ERROR}; resuming once", res.stderr)
        first, second = self.calls()
        self.assertEqual((first["session"], second["session"]), (None, NATIVE))
        self.assertTrue(second["prompt"].startswith(FIX.format(error=UNSAVED_ERROR)))
        self.assertEqual(self.texts("client_session", "client_id"), [NATIVE, NATIVE])
        self.assertEqual(self.read(self.out), DONE["deliverable"])

    def test_a_run_that_starts_no_session_fails(self):
        res = self.drive(said(block(DONE)), unbound=True)
        self.assertNotEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("client_session", self.kinds())
        self.assertIn("no_session", json.dumps(self.records()))


if __name__ == "__main__":
    unittest.main()
