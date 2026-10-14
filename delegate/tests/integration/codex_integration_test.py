"""delegate/cli.py as a subprocess, --client codex, over
session/tests/integration/adapters/codex/fake_codex.py (on PATH as `codex`); HOME, its recovery
store, outside the temp dirs (as the Codex Adapter's tests), the workdir in one, away from any
ancestor's .codex. Main's tests/integration/core/fake_codex_integration_test.py, the Delegate's
part.
"""

import json
import os
import unittest
from unittest import mock

from agent_pm.delegate.engine.runner import FIX

from delegate.tests.integration.cli import (
    DONE,
    NATIVE,
    UNSAVED,
    UNSAVED_ERROR,
    CodexIntegration,
)
from delegate.tests.integration.cli import codex_said as said
from delegate.tests.replies import block, mark
from session.tests.integration.adapters.codex import fake_codex


class Codex(CodexIntegration):
    def test_reports_start_and_valid_outcome(self):
        res = self.drive(said(mark("start", "echoing"), "Codex says hello.", block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        for line in ("Progress (start): echoing", "Codex says hello.", "delegate: status done"):
            self.assertIn(line, res.stderr)
        self.assertEqual(self.read(self.out), "Hello.\n")
        self.assertEqual(
            self.kinds(),
            ["session", "input", "client_session", "session_metadata", "progress", "result", "end"],
        )
        self.assertEqual(self.texts("client_session", "client_id"), [NATIVE])
        (call,) = self.calls()
        self.assertTrue(call["stdin_eof"])
        self.assertEqual(
            (call["cwd"], call["argv"][:2], call["resume_id"]),
            (self.work, ["exec", "--json"], None),
        )
        self.assertIn("# Input\n\nHello.", call["prompt"])

    def test_the_default_policy_reaches_the_argv_as_before_its_move(self):
        res = self.drive(said(block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        (call,) = self.calls()
        self.assertEqual(
            call["argv"][:9],
            [
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-s",
                "danger-full-access",
                "-c",
                'approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ],
        )
        self.assertNotIn("-s", call["argv"][9:])
        self.assertFalse([w for w in call["argv"] if w.startswith("sandbox_workspace_write.")])

    def test_two_runs_resume_the_same_native_session_and_keep_previous_progress(self):
        first = self.drive(said(mark("start", "first turn"), block(DONE)))
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.drive(said(mark("review", "second turn"), block(DONE)), "--resume")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual([call["resume_id"] for call in self.calls()], [None, NATIVE])
        self.assertEqual(self.texts("progress"), ["first turn", "second turn"])
        self.assertEqual(self.texts("client_session", "client_id"), [NATIVE, NATIVE])

    def test_interrupted_first_turn_resumes_the_same_run_and_keeps_progress(self):
        first = self.drive(said(mark("start", "before interruption")))
        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertIn("delegate: the agent run returned no outcome", first.stderr)
        self.assertFalse(os.path.exists(self.out))
        second = self.drive(said(mark("review", "recovered"), block(DONE)), "--resume")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.calls()[-1]["resume_id"], NATIVE)
        self.assertEqual(self.texts("client_session", "client_id"), [NATIVE, NATIVE])
        self.assertEqual(self.texts("progress"), ["before interruption", "recovered"])
        self.assertEqual(len([e for e in self.records() if "outcome" in e]), 1)
        self.assertEqual(self.read(self.out), DONE["deliverable"])

    def test_missing_mapping_fails_before_spawn(self):
        res = self.drive([], "--resume")
        self.assertEqual((res.returncode, res.stdout), (2, ""), res.stderr)
        self.assertIn("delegate: missing Codex recovery state", res.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(os.listdir(self.work), [])

    def test_missing_native_file_fails_before_spawn(self):
        first = self.drive(said(block(DONE)))
        self.assertEqual(first.returncode, 0, first.stderr)
        with mock.patch.dict(os.environ, {"CODEX_HOME": self.env["CODEX_HOME"]}):
            os.unlink(fake_codex.session_path(NATIVE))
        res = self.drive([], "--resume")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("delegate: missing Codex session file", res.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_a_bare_json_final_reply_is_no_outcome(self):
        reply = json.dumps(DONE)
        res = self.drive(said(mark("start", "working"), reply))
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn(reply, res.stderr)
        self.assertIn("delegate: the agent run returned no outcome", res.stderr)
        self.assertEqual(self.records()[-2]["error"], "the agent run returned no outcome")
        self.assertFalse(os.path.exists(self.out))

    def test_an_invalid_outcome_is_fixed_by_one_resume(self):
        res = self.drive(said(mark("start", "working"), block(UNSAVED)), resume=said(block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"delegate: invalid outcome: {UNSAVED_ERROR}; resuming once", res.stderr)
        first, second = self.calls()
        self.assertEqual((first["resume_id"], second["resume_id"]), (None, NATIVE))
        self.assertTrue(second["prompt"].startswith(FIX.format(error=UNSAVED_ERROR)))
        self.assertEqual(
            self.kinds(),
            [
                "session",
                "input",
                "client_session",
                "session_metadata",
                "progress",
                "invalid",
                "session",
                "client_session",
                "result",
                "end",
            ],
        )
        self.assertEqual(self.read(self.out), DONE["deliverable"])


if __name__ == "__main__":
    unittest.main()
