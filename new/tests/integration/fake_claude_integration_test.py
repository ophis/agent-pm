"""new/tests/fake_claude.py run as a process in print mode (its docstring: the format): a bad scenario exits 1 before
any session file or output; valid steps print their stream-json lines; a write step writes its file; a resume runs the
scenario's resume steps."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from new.tests import fake_claude

FAKE = os.path.abspath(fake_claude.__file__)
SID = "11111111-2222-4333-8444-555555555555"
ARGS = ["-p", "hi", "--session-id", SID, "--output-format", "stream-json", "--verbose"]
RESUME = ["-p", "hi", "--resume", SID, "--output-format", "stream-json", "--verbose"]


class BadScenario(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.config = os.path.join(self.root, "config")
        self.scenario = os.path.join(self.root, "scenario.json")
        self.env = {"PATH": "/usr/bin:/bin", "HOME": self.root, "CLAUDE_CONFIG_DIR": self.config,
                    fake_claude.ENV: self.scenario}

    def run_fake(self, args=ARGS, **scenario) -> subprocess.CompletedProcess:
        with open(self.scenario, "w", encoding="utf-8") as f:
            json.dump(scenario, f)
        return subprocess.run([sys.executable, "-I", FAKE, *args], cwd=self.root, env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def result(self, res: subprocess.CompletedProcess) -> str:
        """The result line's text of a run that exited 0."""
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout.splitlines()[-1])["result"]

    def test_a_bad_step_or_value_exits_1_before_the_session(self):
        for scenario in ({"steps": "x"}, {"steps": [1]}, {"steps": [{"kind": "nope"}]}, {"steps": [{"kind": "tool"}]},
                         {"steps": [{"kind": "tool", "name": 7}]}, {"steps": [{"kind": "tool", "name": "R", "x": 1}]},
                         {"steps": [{"kind": "limit"}]}, {"steps": [{"kind": "limit", "resets_at": "soon"}]},
                         {"steps": [{"kind": "limit", "resets_at": True}]}, {"steps": [{"kind": "write", "path": "a"}]},
                         {"steps": [{"kind": "write", "path": 1, "text": "x"}]},
                         {"steps": [{"kind": "write", "path": "", "text": "x"}]},
                         {"steps": [{"kind": "write", "path": "a", "text": None}]}, {"resume": "x"}, {"resume": [1]},
                         {"rate_limit": []}, {"exit": "0"}, {"exit": True}, {"hang": 1}, {"log": 5}):
            with self.subTest(scenario=scenario):
                res = self.run_fake(**scenario)
                self.assertEqual((res.returncode, res.stdout), (1, ""), res.stderr)
                self.assertIn("fake_claude.py: bad scenario", res.stderr)
                self.assertFalse(os.path.exists(self.config))

    def test_valid_steps(self):
        res = self.run_fake(steps=["Hi.", {"kind": "tool", "name": "Read"}, {"kind": "limit", "resets_at": None},
                                   {"kind": "limit", "resets_at": 1.5}])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([json.loads(line)["type"] for line in res.stdout.splitlines()],
                         ["system", "assistant", "assistant", "rate_limit_event", "assistant", "rate_limit_event",
                          "assistant", "result"])
        self.assertTrue(os.path.isdir(os.path.join(self.config, "projects")))

    def test_a_write_step_writes_its_file_under_the_cwd_then_the_next_step_runs(self):
        res = self.run_fake(steps=[{"kind": "write", "path": "sub/out.md", "text": "# Out\n"}, "After."])
        self.assertEqual(self.result(res), "After.")
        self.assertEqual([json.loads(line)["type"] for line in res.stdout.splitlines()],
                         ["system", "assistant", "result"])
        with open(os.path.join(self.root, "sub", "out.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "# Out\n")

    def test_resume_steps_replace_steps_only_on_a_resume(self):
        scenario = {"steps": ["First."], "resume": ["Again."]}
        self.assertEqual(self.result(self.run_fake(**scenario)), "First.")
        self.assertEqual(self.result(self.run_fake(RESUME, **scenario)), "Again.")
        self.assertEqual(self.result(self.run_fake(RESUME, steps=["First."])), "First.")


if __name__ == "__main__":
    unittest.main()
