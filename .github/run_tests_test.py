""".github/run_tests.py: annotations, the step summary and crash detection, in-process and as CI
runs it.
"""

import contextlib
import importlib.util
import io
import json
import os
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


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.summary, self.state = self.tmp / "summary.md", self.tmp / "state"
        env = {
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "RUN_TESTS_STATE": str(self.state),
            "RUN_TESTS_DURATIONS": str(self.tmp / "durations.jsonl"),
        }
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
