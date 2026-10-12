"""delegate/cli.py as a subprocess, --runtime codex, over new/tests/integration/adapters/codex/fake_codex.py (on PATH as
`codex`); HOME, its recovery store, outside the temp dirs (as the Codex Adapter's tests), the workdir in one, away from
any ancestor's .codex. Main's tests/integration/core/fake_codex_integration_test.py, the Delegate's part."""

from new.tests.case import NEW, Case, child_env
from new.tests.integration.adapters.codex import fake_codex
from new.tests.integration.delegate.cli_integration_test import (
    CLI,
    DONE,
    SID,
    UNSAVED,
    UNSAVED_ERROR,
    mark,
)
from new.tests.unit.delegate.runner_test import block

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_pm.delegate.runner import FIX

NATIVE = fake_codex.NATIVE
END = {"out": {"type": "turn.completed"}}


def said(*texts) -> list[dict]:
    """The fake's steps: an agent message per text, then the turn's end."""
    return [*map(fake_codex.message, texts), END]


class Integration(Case):
    def setUp(self):
        home = tempfile.TemporaryDirectory(dir=os.environ.get("AGENT_PM_TEST_TMP", NEW))
        tmp = tempfile.TemporaryDirectory()
        for d in (home, tmp):
            self.addCleanup(d.cleanup)
        self.home, root = os.path.realpath(home.name), os.path.realpath(tmp.name)
        self.work, bin_dir = os.path.join(root, "work"), os.path.join(root, "bin")
        for d in (self.work, bin_dir):
            os.mkdir(d)
        codex = os.path.join(bin_dir, "codex")
        with open(codex, "w") as f:
            f.write(
                f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(fake_codex.__file__)} "$@"\n'
            )
        os.chmod(codex, 0o755)
        self.out = os.path.join(self.work, "out.md")
        self.scenario, self.log = (
            os.path.join(root, "scenario.json"),
            os.path.join(root, "exec.jsonl"),
        )
        self.env = {
            **child_env({}),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": self.home,
            "PYTHONUTF8": "1",
            "CODEX_HOME": os.path.join(self.home, ".codex"),
            "FAKE_LOG": self.log,
            "FAKE_SCENARIO": self.scenario,
        }

    def drive(self, steps, *extra, **scenario) -> subprocess.CompletedProcess:
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, **scenario}, f)
        argv = [
            *CLI,
            "--runtime",
            "codex",
            "--role",
            "dummy-tester",
            "--task",
            "echo",
            "--input",
            "Hello.",
            "--out",
            self.out,
            "--workdir",
            self.work,
            "--sid",
            SID,
            *extra,
        ]
        return subprocess.run(
            argv,
            cwd=self.work,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def records(self) -> list[dict]:
        with open(os.path.join(self.work, "run.jsonl")) as f:
            return [json.loads(line) for line in f]

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.records()]

    def texts(self, kind: str, key: str = "text") -> list:
        return [e[key] for e in self.records() if e["kind"] == kind]

    def calls(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def read(self, path: str) -> str:
        with open(path) as f:
            return f.read()


class Codex(Integration):
    def test_reports_start_and_valid_outcome(self):
        res = self.drive(said(mark("start", "echoing"), "Codex says hello.", block(DONE)))
        self.assertEqual(res.returncode, 0, res.stderr)
        for line in ("Progress (start): echoing", "Codex says hello.", "delegate: status done"):
            self.assertIn(line, res.stderr)
        self.assertEqual(self.read(self.out), "Hello.\n")
        self.assertEqual(
            self.kinds(), ["session", "input", "client_session", "progress", "result", "end"]
        )
        self.assertEqual(self.texts("client_session", "client_id"), [NATIVE])
        (call,) = self.calls()
        self.assertTrue(call["stdin_eof"])
        self.assertEqual(
            (call["cwd"], call["argv"][:2], call["resume_id"]),
            (self.work, ["exec", "--json"], None),
        )
        self.assertIn("# Input\n\nHello.", call["prompt"])

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
