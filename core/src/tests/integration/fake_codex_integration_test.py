"""Process checks for the Codex headless client, with a hermetic Codex CLI."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import fake_codex


CORE = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DRIVE = os.path.join(CORE, "src", "drive.py")
SID = "12345678-1234-1234-1234-123456789abc"
CLIENT_ID = "11111111-2222-4333-8444-555555555555"
DONE = {"status": "done", "title": "echo", "summary": "Hello.", "deliverable": "Hello.\n"}


class CodexIntegration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.home, self.work, bin_dir = (os.path.join(self.root, d) for d in ("home", "work", "bin"))
        for path in (self.home, self.work, bin_dir):
            os.mkdir(path)
        self.out = os.path.join(self.work, "out.md")
        self.scenario = os.path.join(self.root, "scenario.json")
        self.log = os.path.join(self.root, "codex-call.json")
        self.env = {"PATH": fake_codex.install(bin_dir), "HOME": self.home, "PYTHONUTF8": "1",
                    fake_codex.ENV: self.scenario}

    def drive(self, steps, *extra, **scenario):
        with open(self.scenario, "w") as f:
            json.dump({"steps": steps, "log": self.log, **scenario}, f)
        argv = [sys.executable, DRIVE, "--client", "codex", "--role", "dummy-tester", "--task", "echo",
                "--input", "Hello.", "--out", self.out, "--workdir", self.work, "--sid", SID, *extra]
        return subprocess.run(argv, cwd=self.work, env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30)

    def events(self):
        with open(os.path.join(self.work, "run.jsonl")) as f:
            return [json.loads(line) for line in f]

    def test_reports_start_and_valid_outcome_through_channel(self):
        res = self.drive([{"kind": "progress", "name": "start", "text": "echoing"},
                          "Codex says hello.", {"kind": "outcome", "outcome": DONE}])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Progress (start): echoing", res.stderr)
        self.assertIn("Codex says hello.", res.stderr)
        self.assertIn("status done", res.stderr)
        with open(self.out) as f:
            self.assertEqual(f.read(), "Hello.\n")
        self.assertEqual([e["kind"] for e in self.events()],
                         ["input", "session", "client_session", "progress", "outcome", "result", "end"])
        with open(self.log) as f:
            call = json.load(f)
        self.assertTrue(call["stdin_eof"])
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.work))
        self.assertEqual(call["argv"][0:2], ["exec", "--json"])
        self.assertNotIn("--sandbox", call["argv"])
        self.assertIn("--skip-git-repo-check", call["argv"])
        self.assertIn("--approve-for-me", call["argv"])
        self.assertIn("# Input\n\nHello.", call["prompt"])
        self.assertEqual([e["client_id"] for e in self.events() if e["kind"] == "client_session"], [CLIENT_ID])

    def test_two_turns_resume_the_same_native_session_and_keep_previous_progress(self):
        first = self.drive([{"kind": "progress", "name": "start", "text": "first turn"},
                            {"kind": "outcome", "outcome": DONE}])
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.drive([{"kind": "progress", "name": "review", "text": "second turn"},
                             {"kind": "outcome", "outcome": DONE}], "--resume")
        self.assertEqual(second.returncode, 0, second.stderr)
        with open(self.log) as f:
            call = json.load(f)
        self.assertEqual(call["resume_id"], CLIENT_ID)
        self.assertLess(call["argv"].index("--approve-for-me"), call["argv"].index("resume"))
        self.assertEqual([e["text"] for e in self.events() if e["kind"] == "progress"],
                         ["first turn", "second turn"])
        self.assertEqual([e["client_id"] for e in self.events() if e["kind"] == "client_session"],
                         [CLIENT_ID, CLIENT_ID])

    def test_missing_native_file_fails_before_fake_spawn(self):
        first = self.drive([{"kind": "outcome", "outcome": DONE}])
        self.assertEqual(first.returncode, 0, first.stderr)
        os.unlink(self.log)
        codex_home = os.path.join(self.home, ".codex", "sessions")
        for root, _, files in os.walk(codex_home):
            for name in files:
                os.unlink(os.path.join(root, name))
        second = self.drive([], "--resume")
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertIn("missing Codex session file", second.stderr)
        self.assertFalse(os.path.exists(self.log))

    def test_missing_mapping_fails_before_fake_spawn(self):
        res = self.drive([], "--resume")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("missing Codex recovery state", res.stderr)
        self.assertFalse(os.path.exists(self.log))

    def test_different_native_id_on_resume_is_rejected(self):
        first = self.drive([{"kind": "outcome", "outcome": DONE}])
        self.assertEqual(first.returncode, 0, first.stderr)
        changed = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        second = self.drive([{"kind": "outcome", "outcome": DONE}], "--resume", thread_id=changed)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already bound to a different client ID", second.stderr)
        self.assertEqual([e["client_id"] for e in self.events() if e["kind"] == "client_session"], [CLIENT_ID])

    def test_missing_outcome_is_not_inferred_from_final_reply(self):
        reply = json.dumps(DONE)
        res = self.drive([{"kind": "progress", "name": "start", "text": "working"}, reply])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn(reply, res.stderr)
        self.assertIn("the agent run returned no outcome", res.stderr)
        self.assertEqual(self.events()[-2]["error"], "the agent run returned no outcome")
        self.assertFalse(os.path.exists(self.out))

    def test_invalid_report_fails_validation(self):
        invalid = {k: v for k, v in DONE.items() if k != "deliverable"}
        res = self.drive([{"kind": "progress", "name": "start", "text": "working"},
                          {"kind": "outcome", "outcome": invalid}])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("invalid outcome: done with a local destination must carry the deliverable", res.stderr)
        self.assertIn("invalid outcome", self.events()[-2]["error"])
        self.assertFalse(os.path.exists(self.out))

    def test_stderr_is_readable_and_recorded(self):
        res = self.drive([{"kind": "stderr", "text": "Codex diagnostic"},
                          {"kind": "progress", "name": "start", "text": "working"},
                          {"kind": "outcome", "outcome": DONE}])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Codex diagnostic", res.stderr)
        self.assertIn("Codex diagnostic", [e["text"] for e in self.events() if e["kind"] == "stderr"])

    def test_tui_rejected_before_fake_codex_starts(self):
        res = self.drive([], "--runner", "tui")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("CodexClient has no tui command", res.stderr)
        self.assertFalse(os.path.exists(self.log))


if __name__ == "__main__":
    unittest.main()
