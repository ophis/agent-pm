"""Test supervision in isolated child interpreters."""

import subprocess
import sys
import tempfile
from pathlib import Path

from runner_fixtures import HERE, SCRIPT

from tests.case import Case
from utils.tests.sandbox import clean_env


class Supervise(Case):
    def supervise(self, mode):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "job.py").write_text(SCRIPT.format(github=str(HERE), mode=mode))
        env = {
            k: v
            for k, v in clean_env(None, str(HERE.parent), str(HERE)).items()
            if k != "RUN_TESTS_STATE"
        }
        env["GITHUB_STEP_SUMMARY"] = str(tmp / "summary.md")
        env["RUN_TESTS_DURATIONS"] = str(tmp / "durations.jsonl")
        proc = subprocess.run(
            [sys.executable, str(tmp / "job.py")], env=env, capture_output=True, text=True
        )
        summary = tmp / "summary.md"
        return proc, summary.read_text() if summary.exists() else ""

    def test_a_crash_after_all_tests_pass_is_annotated_with_the_last_test_and_stack(self):
        proc, summary = self.supervise("after")
        self.assertEqual(proc.returncode, 134)
        message = "interpreter crashed after all tests passed (exit 134); last test started: "
        self.assertIn(f"::error title=Unit x::{message}__main__.T.test_a%0A", proc.stdout)
        self.assertIn("Fatal Python error: Aborted", proc.stdout)
        self.assertIn(message, summary)

    def test_a_crash_in_a_test_names_that_test(self):
        proc, _ = self.supervise("mid")
        self.assertEqual(proc.returncode, 134)
        self.assertIn(
            "::error title=Unit x::interpreter crashed (exit 134) in __main__.T.test_a", proc.stdout
        )
        self.assertIn("in test_a", proc.stdout)

    def test_an_exit_before_any_test_is_annotated(self):
        proc, _ = self.supervise("early")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("::error title=Unit x::exited 2 before any test started", proc.stdout)

    def test_a_plain_failure_or_success_adds_no_crash_annotation(self):
        for mode, code, annotations in (("fail", 1, 1), ("ok", 0, 0)):
            with self.subTest(mode=mode):
                proc, _ = self.supervise(mode)
                self.assertEqual(proc.returncode, code)
                self.assertEqual(proc.stdout.count("::error"), annotations)
                self.assertNotIn("crashed", proc.stdout)

    def test_main_discovers_from_the_cwd(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "pkg").mkdir()
        (tmp / "pkg" / "__init__.py").write_text("")
        (tmp / "pkg" / "x_test.py").write_text(
            "import unittest\n\n\nclass X(unittest.TestCase):\n    def test_x(self):\n        self.fail('no')\n"
        )
        env = {
            k: v
            for k, v in clean_env(None, str(HERE.parent), str(HERE)).items()
            if k not in ("RUN_TESTS_STATE", "GITHUB_STEP_SUMMARY")
        }
        env["RUN_TESTS_DURATIONS"] = str(tmp / "durations.jsonl")
        proc = subprocess.run(
            [sys.executable, str(HERE / "run_tests.py"), "Unit pkg", "pkg"],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "::error title=pkg.x_test.X.test_x::AssertionError: no\n")
