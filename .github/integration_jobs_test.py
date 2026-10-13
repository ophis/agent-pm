""".github/integration_jobs.py over temp test trees and crafted ids, then over the real repo (import only)."""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "integration_jobs.py")
spec = importlib.util.spec_from_file_location("integration_jobs", PATH)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

PASS = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n"
FAIL = PASS.replace("pass", "self.fail('no')")
JOBS = {"a": ["a/"], "b": ["b/"]}
FORMAT = (
    r"^workflow: the integration job's matrix\.job must be one inline JSON array of unique strings$"
)


def workflow(matrix):
    return (
        'jobs:\n  unit:\n    strategy:\n      matrix:\n        job: ["a", "b"]\n'
        f"  integration:\n    strategy:\n      matrix:\n{matrix}\n    steps: []\n"
    )


WORKFLOW = workflow('        job: ["a", "b"]')


class Tree(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.package = self.root.name
        self.enterContext(mock.patch.object(sys, "path", list(sys.path)))
        self.addCleanup(self.forget)

    def forget(self):
        for name in [n for n in sys.modules if n.split(".")[0] == self.package]:
            del sys.modules[name]

    def write(self, files):
        for name, text in files.items():
            path = self.root / self.package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            for folder in [path.parent, *path.parent.parents][: len(Path(name).parts)]:
                (folder / "__init__.py").touch()
            path.write_text(text)

    def at(self, jobs):
        return {job: [f"{self.package}/{e}" for e in entries] for job, entries in jobs.items()}

    def check(self, jobs=JOBS, text=WORKFLOW):
        split = gate.check(self.root, (self.package,), self.at(jobs), text)
        prefix = f"{self.package}."
        return {
            job: sorted(type(test).__module__.removeprefix(prefix) for test in tests)
            for job, tests in split.items()
        }


class Assignment(Tree):
    def test_classes_in_one_file_can_belong_to_different_jobs(self):
        self.write({"x_test.py": PASS + PASS.replace("class T", "class U")})
        jobs = {"a": ["x_test.py::T"], "b": ["x_test.py::U"]}
        split = gate.check(self.root, (self.package,), self.at(jobs), WORKFLOW)
        self.assertEqual(
            {job: [type(test).__name__ for test in suite] for job, suite in split.items()},
            {"a": ["T"], "b": ["U"]},
        )

    def test_a_class_overrides_file_and_directory_entries(self):
        source = PASS + PASS.replace("class T", "class VeryLongClassName")
        self.write({"a/x_test.py": source})
        for fallback in ("a/", "a/x_test.py", "a/x_test.py::VeryLongClassName"):
            jobs = {"a": [fallback], "b": ["a/x_test.py::T"]}
            with self.subTest(fallback=fallback):
                split = gate.check(self.root, (self.package,), self.at(jobs), WORKFLOW)
                self.assertEqual(
                    {job: [type(test).__name__ for test in suite] for job, suite in split.items()},
                    {"a": ["VeryLongClassName"], "b": ["T"]},
                )

    def test_a_class_in_two_jobs_fails(self):
        self.write({"a/x_test.py": PASS, "b/y_test.py": PASS})
        with self.assertRaisesRegex(
            ValueError, rf"duplicate=\['{self.package}\.a\.x_test\.T\.test_a'\]$"
        ):
            self.check({"a": ["a/x_test.py::T"], "b": ["b/", "a/x_test.py::T"]})

    def test_a_missing_class_fails_even_with_a_matching_file_or_directory(self):
        self.write({"a/x_test.py": PASS, "b/y_test.py": PASS})
        for fallback in ("a/", "a/x_test.py"):
            with (
                self.subTest(fallback=fallback),
                self.assertRaisesRegex(
                    ValueError, rf"^no integration tests for: {self.package}/a/x_test\.py::Missing$"
                ),
            ):
                self.check({"a": [fallback, "a/x_test.py::Missing"], "b": ["b/"]})

    def test_an_unspecified_class_fails_naming_its_file(self):
        self.write({"a/x_test.py": PASS + PASS.replace("class T", "class U"), "b/y_test.py": PASS})
        with self.assertRaisesRegex(
            ValueError, rf"^no integration job for: {self.package}/a/x_test\.py$"
        ):
            self.check({"a": ["a/x_test.py::T"], "b": ["b/"]})

    def test_an_unassigned_file_fails_naming_its_path(self):
        self.write(
            {"a/x_test.py": PASS, "b/y_test.py": PASS, "new_test.py": PASS, "c/d/z_test.py": PASS}
        )
        with self.assertRaisesRegex(
            ValueError,
            rf"^no integration job for: {self.package}/c/d/z_test\.py, {self.package}/new_test\.py$",
        ):
            self.check()

    def test_the_longest_entry_wins(self):
        self.write({"a/x_test.py": PASS, "a/y_test.py": PASS, "a/c/z_test.py": PASS})
        self.write({"b/w_test.py": PASS})
        jobs = {"a": ["a/"], "b": ["b/", "a/y_test.py", "a/c/"]}
        self.assertEqual(
            self.check(jobs), {"a": ["a.x_test"], "b": ["a.c.z_test", "a.y_test", "b.w_test"]}
        )

    def test_a_file_moved_into_an_assigned_directory_passes_and_its_stale_entry_too(self):
        self.write({"a/c/f_test.py": PASS, "b/y_test.py": PASS})
        jobs = {"a": ["a/", "f_test.py"], "b": ["b/"]}
        self.assertEqual(self.check(jobs), {"a": ["a.c.f_test"], "b": ["b.y_test"]})

    def test_an_empty_job_fails(self):
        self.write({"a/x_test.py": PASS})
        with self.assertRaisesRegex(
            ValueError, r"empty=\['b'\], missing=\[\], stale=\[\], duplicate=\[\]$"
        ):
            self.check()

    def test_an_entry_in_two_jobs_puts_its_tests_in_both_and_fails(self):
        self.write({"a/x_test.py": PASS, "b/y_test.py": PASS})
        with self.assertRaisesRegex(
            ValueError, rf"duplicate=\['{self.package}\.a\.x_test\.T\.test_a'\]$"
        ):
            self.check({"a": ["a/"], "b": ["b/", "a/"]})

    def test_a_loader_error_fails(self):
        self.write({"a/x_test.py": "import integration_jobs_test_absent\n", "b/y_test.py": PASS})
        with self.assertRaisesRegex(
            ValueError, rf"Failed to import test module: {self.package}\.a\.x_test"
        ):
            self.check()

    def test_a_workflow_matrix_unlike_the_table_fails(self):
        self.write({"a/x_test.py": PASS, "b/y_test.py": PASS})
        differs = r"^workflow matrix\.job \['a'(, 'b', 'c')?\] differs from JOBS \['a', 'b'\]$"
        for matrix, error in (
            ('        job: ["a"]', differs),
            ('        job: ["a", "b", "c"]', differs),
            ('        job: ["a", "b", "a"]', FORMAT),
            ("        job: [a, b]", FORMAT),
            ('        job: ["a", 1]', FORMAT),
            ("        job:\n          - a\n          - b", FORMAT),
            ('        job: ["a"]\n        job: ["b"]', FORMAT),
            ("", FORMAT),
        ):
            with self.subTest(matrix=matrix), self.assertRaisesRegex(ValueError, error):
                self.check(text=workflow(matrix))

    def test_a_job_runs_only_its_tests_verbosely_and_returns_1_on_a_failure(self):
        self.write({"a/x_test.py": PASS, "b/y_test.py": FAIL})
        split = gate.check(self.root, (self.package,), self.at(JOBS), WORKFLOW)
        for job, other, code in (("a", "b", 0), ("b", "a", 1)):
            err = io.StringIO()
            with self.subTest(job=job), contextlib.redirect_stderr(err):
                self.assertEqual(gate.run(split[job]), code)
            self.assertIn(f"test_a ({self.package}.{job}.", err.getvalue())
            self.assertNotIn(f"{self.package}.{other}.", err.getvalue())
            self.assertIn("Ran 1 test", err.getvalue())


class Coverage(unittest.TestCase):
    def test_a_test_id_in_two_jobs_or_in_none_fails(self):
        found = {"m.T.test_a", "m.T.test_b"}
        for suites, error in (
            (
                {"a": ["m.T.test_a"], "b": ["m.T.test_a", "m.T.test_b"]},
                r"duplicate=\['m\.T\.test_a'\]$",
            ),
            (
                {"a": ["m.T.test_a"], "b": ["m.T.test_c"]},
                r"missing=\['m\.T\.test_b'\], stale=\['m\.T\.test_c'\]",
            ),
        ):
            with self.subTest(suites=suites), self.assertRaisesRegex(ValueError, error):
                gate.validate(found, suites, ["a", "b"])
        gate.validate(found, {"a": ["m.T.test_a"], "b": ["m.T.test_b"]}, ["b", "a"])


class Repo(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(sys, "path", list(sys.path)))

    def test_every_integration_test_lands_in_exactly_one_job(self):
        split = gate.check(gate.ROOT, gate.DIRECTORIES, gate.JOBS, gate.WORKFLOW.read_text())
        loader = unittest.TestLoader()
        found = [
            test
            for directory in gate.DIRECTORIES
            for test in gate.tests(
                loader.discover(
                    str(gate.ROOT / directory), pattern="*_test.py", top_level_dir=str(gate.ROOT)
                )
            )
        ]
        self.assertEqual(loader.errors, [])
        self.assertEqual(
            Counter(test.id() for tests in split.values() for test in tests),
            Counter(test.id() for test in found),
        )

    def test_main_checks_or_names_the_jobs(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gate.main(["--check"]), 0)
        self.assertRegex(
            out.getvalue(), r"^integration jobs match discovery and the workflow: session 1 \d+, "
        )
        for argv in ([], ["nope"], ["session"], ["manager", "1"]):
            with (
                self.subTest(argv=argv),
                self.assertRaisesRegex(
                    ValueError,
                    "^give --check or a job name: session 1, session 2, delegate, linear-orchestrator, ",
                ),
            ):
                gate.main(argv)
