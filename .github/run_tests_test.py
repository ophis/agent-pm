""".github/run_tests.py: annotations, the step summary and crash detection, in-process and as CI
runs it.
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("run_tests", HERE / "run_tests.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Cases:
    def test_pass(self):
        pass

    def test_fail(self):
        self.assertEqual(1, 2, "one\ntwo")

    def test_error(self):
        raise KeyError("k")

    def test_sub(self):
        with self.subTest(n=1):
            self.fail("a:b, c% |")


# A real os.abort() makes macOS file a crash report per run: print faulthandler's report and exit
# 134 instead.
SCRIPT = """import faulthandler, os, sys, unittest
sys.path.insert(0, {github!r})
import run_tests

def abort():
    sys.stderr.write("Fatal Python error: Aborted\\n\\n")
    sys.stderr.flush()
    faulthandler.dump_traceback()
    os._exit(134)

class T(unittest.TestCase):
    def test_a(self):
        if {mode!r} == "mid":
            abort()
        self.assertNotEqual({mode!r}, "fail")

code = run_tests.supervise("Unit x")
if code is None:
    if {mode!r} == "early":
        sys.exit(2)
    code = run_tests.run(unittest.TestLoader().loadTestsFromTestCase(T), "Unit x")
    if {mode!r} == "after":
        abort()
sys.exit(code)
"""


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.summary, self.state = self.tmp / "summary.md", self.tmp / "state"
        env = {"GITHUB_STEP_SUMMARY": str(self.summary), "RUN_TESTS_STATE": str(self.state)}
        self.enterContext(mock.patch.dict(os.environ, env))

    def run_tests(self, *names):
        out = io.StringIO()
        case = type("T", (Cases, unittest.TestCase), {})
        suite = unittest.TestSuite(case(name) for name in names)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = runner.run(suite, "Unit x")
        return code, out.getvalue()

    def test_failures_and_errors_are_annotated_by_test_id_and_tabled_in_the_summary(self):
        code, out = self.run_tests("test_pass", "test_fail", "test_error", "test_sub")
        self.assertEqual(code, 1)
        self.assertEqual(
            out.splitlines(),
            [
                f"::error title={__name__}.T.test_fail::AssertionError: 1 != 2 : one",
                f"::error title={__name__}.T.test_error::KeyError: 'k'",
                f"::error title={__name__}.T.test_sub (n=1)::AssertionError: a:b, c%25 |",
            ],
        )
        summary = self.summary.read_text()
        self.assertTrue(summary.startswith("### Unit x: 3 failed\n\n| Test | Error |\n"))
        self.assertIn(f"| `{__name__}.T.test_error` | KeyError: 'k' |", summary)
        self.assertIn(f"| `{__name__}.T.test_sub (n=1)` | AssertionError: a:b, c% \\| |", summary)
        self.assertEqual(self.state.read_text().splitlines()[-1], "failed")

    def test_success_prints_and_summarizes_nothing(self):
        self.assertEqual(self.run_tests("test_pass"), (0, ""))
        self.assertFalse(self.summary.exists())
        self.assertEqual(self.state.read_text(), f"start {__name__}.T.test_pass\npassed\n")

    def test_durations_are_recorded_outside_the_checkout(self):
        timings = self.tmp / "durations.jsonl"
        with mock.patch.dict(os.environ, {"RUN_TESTS_DURATIONS": str(timings)}):
            self.run_tests("test_pass")

        row = json.loads(timings.read_text())
        self.assertEqual(row["test"], f"{__name__}.T.test_pass")
        self.assertGreaterEqual(row["seconds"], 0)

    def test_annotation_text_is_escaped(self):
        self.assertEqual(runner.escape("a%\r\n:,"), "a%25%0D%0A:,")
        self.assertEqual(runner.escape("a%\r\n:,", prop=True), "a%25%0D%0A%3A%2C")


class Supervise(unittest.TestCase):
    def supervise(self, mode):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "job.py").write_text(SCRIPT.format(github=str(HERE), mode=mode))
        env = {k: v for k, v in os.environ.items() if k != "RUN_TESTS_STATE"}
        env["GITHUB_STEP_SUMMARY"] = str(tmp / "summary.md")
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
            "import unittest\n\n\nclass X(unittest.TestCase):\n"
            "    def test_x(self):\n        self.fail('no')\n"
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("RUN_TESTS_STATE", "GITHUB_STEP_SUMMARY")
        }
        proc = subprocess.run(
            [sys.executable, str(HERE / "run_tests.py"), "Unit pkg", "pkg"],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "::error title=pkg.x_test.X.test_x::AssertionError: no\n")


class Discovery(unittest.TestCase):
    def test_top_level_preserves_project_packages_and_normalized_tests(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        regular = root / "session/tests/unit"
        normalized = root / "linear-orchestrator/tests/orchestrator_tests/unit"
        for path in (regular, normalized):
            path.mkdir(parents=True)
            (path / "__init__.py").touch()
        (normalized.parent / "__init__.py").touch()
        self.assertEqual(runner.discovery_top(root, regular), root.resolve())
        self.assertEqual(runner.discovery_top(root, normalized), normalized.parent.parent.resolve())
        self.assertEqual(
            runner.discovery_top(root, (root / ".github").resolve()), (root / ".github").resolve()
        )
