import argparse
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import launch  # noqa: E402
import pipeline  # noqa: E402

CONFIG = """team = "T"
[projects."Deep Research"]
instructions = "stages/deep-research.md"
model = "opus"
effort = "xhigh"
add_dirs = ["~/playground/private_docs"]
[projects."Product Design"]
prefix = "PRD"
"""
FAKE_CLAUDE = """#!/bin/bash
echo "claude says hi"
echo "oops" >&2
exit 3
"""


class Launch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.config = os.path.join(self.tmp, "pipeline.toml")
        with open(self.config, "w") as f:
            f.write(CONFIG)
        self.runs = os.path.join(self.tmp, "logs", "runs.log")
        self.logs = os.path.join(self.tmp, "logs")
        self.calls = []

    def run_launch(self, *argv):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = launch.main(list(argv), sh=lambda cmd, **kw: self.calls.append(cmd), config=self.config,
                             runs=self.runs, logs=self.logs)
        self.err = err.getvalue()
        return rc

    def args(self, mode="new"):
        base = ["--issue", "TASK-1", "--url", "https://l/TASK-1", "--project", "Deep Research", "--sid", "s1", "--mode", mode]
        return base + (["--k", "2"] if mode == "resume" else [])

    def claude_cmd(self, a):
        return launch.command(a, launch.runnable(pipeline.load_config(self.config))["Deep Research"])

    def parse(self, mode):
        return argparse.Namespace(issue="TASK-1", url="https://l/TASK-1", project="Deep Research", sid="s1", mode=mode, k="2")

    def test_tmux_session_cwd_and_bash(self):
        self.assertEqual(self.run_launch(*self.args()), 0)
        (cmd,) = self.calls
        self.assertEqual(cmd[:7], ["tmux", "new-session", "-d", "-s", "agent-pm", "-c", pipeline.WORK])
        self.assertEqual(cmd[7:9], ["bash", "-c"])
        self.assertIn("export PATH=/opt/homebrew/bin:", cmd[9])
        self.assertIn("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=3600000", cmd[9])
        self.assertIn(os.path.join(self.logs, "projects", "deep-research.log"), cmd[9])

    def test_new_prompt_and_flags(self):
        cmd = self.claude_cmd(self.parse("new"))
        instructions = os.path.join(pipeline.ROOT, "stages/deep-research.md")
        self.assertEqual(cmd[:3], ["claude", "-p", f"Follow {instructions} to handle TASK-1 (https://l/TASK-1). "
                                                  "The runner has already claimed it."])
        self.assertEqual(cmd[3:], ["--session-id", "s1", "--model", "opus", "--effort", "xhigh", "--permission-mode", "auto",
                                   "--add-dir", pipeline.ROOT, "--add-dir", os.path.expanduser("~/playground/private_docs")])

    def test_resume_prompt(self):
        cmd = self.claude_cmd(self.parse("resume"))
        instructions = os.path.join(pipeline.ROOT, "stages/deep-research.md")
        self.assertEqual(cmd[2], f"Resumed run 2 for TASK-1 (https://l/TASK-1) after an interruption. Re-read {instructions} "
                                 "first (it may have changed since this session started) and follow its resume rule.")
        self.assertEqual(cmd[3:5], ["--resume", "s1"])

    def test_unknown_or_non_runnable_project(self):
        for project in ("Nope", "Product Design"):
            argv = self.args()
            argv[argv.index("--project") + 1] = project
            self.assertEqual(self.run_launch(*argv), 2)
            self.assertIn("not a runnable project", self.err)
        self.assertEqual(self.calls, [])

    def test_script_logs_output_and_end_lines(self):
        self.run_launch(*self.args())
        script = self.calls[0][9]
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(FAKE_CLAUDE)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        script = script.replace("export PATH=", f"export PATH={bindir}:", 1)
        subprocess.run(["bash", "-c", script], check=True, capture_output=True)
        with open(os.path.join(self.logs, "projects", "deep-research.log")) as f:
            plog = f.read().splitlines()
        with open(self.runs) as f:
            runs = f.read().splitlines()
        self.assertRegex(plog[0], r"^\S+ \S+ launch TASK-1 mode=new session=s1$")
        self.assertEqual(sorted(plog[1:3]), ["claude says hi", "oops"])
        self.assertRegex(plog[3], r"^\S+ \S+ end TASK-1 session=s1 exit=3$")
        self.assertEqual(runs, [plog[3]])

    def test_env_not_inherited(self):
        with mock.patch.dict(os.environ, {"PATH": "/nowhere"}):
            self.run_launch(*self.args())
        self.assertNotIn("/nowhere", self.calls[0][9])


if __name__ == "__main__":
    unittest.main()
