"""The Delegate CLI as a subprocess run by step 3's command of new/core/skills/act-as/SKILL.md, over the real config with
a HOME of the test's own: per built-in role, out (or stdout, --out -) holds the --dry-run prompt; headless and a resume
exit 2."""

import json
import os
import subprocess
import tempfile
import unittest

from new.tests.case import Case
from new.tests.unit.skills.act_as_test import argv, head

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
ROLES = ("researcher", "pm", "engineer", "dummy-tester")
INPUT = "Compare A and B."
STARTED = "Progress (start): the composed prompt is handed to the caller"


class PrintPrompt(Case):
    """The cwd `proj`; per role, workdir ~/.agent-pm/adhoc/<date>-<role>-compare-a-b (not made), --sid SID."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proj = os.path.realpath(tmp.name)
        self.adhoc = os.path.join(self.agent_pm, "adhoc")
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": self.home,
            "PYTHONUTF8": "1",
        }

    def work(self, role) -> str:
        return os.path.join(self.adhoc, f"2026-01-02-{role}-compare-a-b")

    def cli(self, role, *extra, mode="in_process", out=None) -> subprocess.CompletedProcess:
        work = self.work(role)
        args = ["--runtime", "print_prompt", "--mode", mode, "--role", role, "--workdir", work]
        args += ["--out", out or os.path.join(work, "prompt.md"), "--input", INPUT, "--sid", SID]
        return subprocess.run(
            [*argv(head()), *args, *extra],
            cwd=self.proj,
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )

    def dry_run(self, role, out=None) -> str:
        res = self.cli(role, "--dry-run", out=out)
        self.assertEqual(res.returncode, 0, res.stderr)
        return json.loads(res.stdout)["prompt"]

    def test_each_role_is_done_and_out_holds_the_dry_run_prompt(self):
        for role in ROLES:
            with self.subTest(role=role):
                want, out = self.dry_run(role), os.path.join(self.work(role), "prompt.md")
                res = self.cli(role)
                self.assertEqual((res.returncode, res.stdout), (0, ""), res.stderr)
                with open(out, encoding="utf-8") as f:
                    self.assertEqual(f.read(), want)
                lines = res.stderr.splitlines()
                for line in (STARTED, f"delegate: deliverable {out}", "delegate: status done"):
                    self.assertIn(line, lines)
                self.assertEqual(res.stderr.count("Progress ("), 1)
                with open(os.path.join(self.work(role), "run.jsonl")) as f:
                    records = [json.loads(line) for line in f]
                self.assertEqual(
                    [e["kind"] for e in records], ["session", "input", "progress", "result", "end"]
                )
                self.assertEqual(
                    (records[2]["name"], records[3]["outcome"]["status"], records[4]["rc"]),
                    ("start", "done", 0),
                )

    def test_out_dash_prints_the_dry_run_prompt_to_stdout(self):
        for role in ROLES:
            with self.subTest(role=role):
                want = self.dry_run(role, out="-")
                res = self.cli(role, out="-")
                self.assertEqual((res.returncode, res.stdout), (0, want), res.stderr)
                self.assertIn(STARTED, res.stderr.splitlines())
                self.assertNotIn("delegate: deliverable", res.stderr)

    def test_headless_exits_2_stdout_empty_nothing_started(self):
        res = self.cli("researcher", mode="headless", out="-")
        self.assertEqual((res.returncode, res.stdout), (2, ""))
        self.assertIn(
            "delegate: runtime 'print_prompt' does not support headless mode",
            res.stderr.splitlines(),
        )
        self.assertFalse(os.path.exists(self.work("researcher")))

    def test_a_resume_exits_2(self):
        res = self.cli("researcher", "--resume", out="-")
        self.assertEqual((res.returncode, res.stdout), (2, ""))
        self.assertIn("delegate: print_prompt has no session to resume", res.stderr.splitlines())


if __name__ == "__main__":
    unittest.main()
