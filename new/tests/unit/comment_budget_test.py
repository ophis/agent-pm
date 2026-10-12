""".github/comment_budget.py: measure, rule, and main over temp git repos."""

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from new.tests.case import NEW, Case

PATH = os.path.join(os.path.dirname(NEW), ".github", "comment_budget.py")
spec = importlib.util.spec_from_file_location("comment_budget", PATH)
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)

CODE = "def f(x):\n    return [x * 2 for x in range(100) if x % 3]\n" * 20
DOC = '"""' + "A long docstring that only explains what the code already says. " * 20 + '"""\n'
INIT = '"""' + "d" * 494 + '"""\n'
PURPOSE = (
    "The comment budget keeps comments short: meet it by cutting comments or docstrings, "
    "never by adding code to lower the ratio."
)
SOURCE_RULE = "source (15% of code + 500 characters per directory)"
TESTS_RULE = "tests (10% of code + 500 characters per directory)"


def chars(text):
    return len("".join(text.split()))


def over(folder, n, code, limit):
    allowed = limit * code // 100 + 500
    ratio = f"{n / code:.2%}" if code else "inf"
    return (
        f"  {folder}: {ratio} ({n} comment+docstring / {code} code characters; "
        f"{allowed} allowed, {n - allowed} over)"
    )


class Measure(Case):
    def test_comments_count_with_their_hash_but_a_shebang_does_not(self):
        self.assertEqual(
            budget.measure("x = 1  # a note\n"), (chars("# a note"), 0, chars("x = 1"))
        )
        self.assertEqual(
            budget.measure("#!/usr/bin/env python3\nx = 1\n"),
            (0, 0, chars("#!/usr/bin/env python3 x = 1")),
        )
        self.assertEqual(
            budget.measure("x = 1\n#! not a shebang\n"),
            (chars("#! not a shebang"), 0, chars("x = 1")),
        )

    def test_docstrings_and_other_bare_strings_count(self):
        strings = [
            '"""Module."""',
            "'''Class\n    over two lines.'''",
            '"Function."',
            '"inside an if"',
        ]
        text = (
            f"{strings[0]}\nclass A:\n    {strings[1]}\n    def f(self):\n        {strings[2]}\n"
            f"        if self:\n            {strings[3]}\n        return 1\n"
        )
        comment, docstring, code = budget.measure(text)
        self.assertEqual((comment, docstring), (0, sum(map(chars, strings))))
        self.assertEqual(code, chars(text) - docstring)

    def test_ordinary_strings_are_code(self):
        text = (
            'x = "an error message"\nprint("help text", x)\ny = f"{x} more"\n'
            'DOC = """assigned, not bare"""\ndef f():\n    return "a value"\n'
        )
        self.assertEqual(budget.measure(text), (0, 0, chars(text)))

    def test_code_is_every_other_character(self):
        text = '"""Doc."""\nx = "s"  # note\n'
        comment, docstring, code = budget.measure(text)
        self.assertEqual(
            (comment, docstring, comment + docstring + code),
            (chars("# note"), chars('"""Doc."""'), chars(text)),
        )


class Rules(Case):
    def test_paths_pick_their_rule(self):
        for path in ("tests/a.py", "x/tests/b.py", "new/tests/c.py", "core/d_test.py"):
            with self.subTest(path=path):
                self.assertIs(budget.rule(path), budget.TESTS)
        for path in ("new/core/src/e.py", ".github/comment_budget.py", "contests/f.py", "tests.py"):
            with self.subTest(path=path):
                self.assertIs(budget.rule(path), budget.SOURCE)


class Main(Case):
    def run_in(self, tracked, untracked=()):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for path, text in {**tracked, **dict(untracked)}.items():
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(text)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "--", *tracked], cwd=root, check=True)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = budget.main(root)
            return code, out.getvalue().splitlines(), err.getvalue()

    def test_under_budget_passes_with_a_summary_per_rule(self):
        code, lines, err = self.run_in(
            {"x.py": INIT, "core/a.py": CODE, "core/sub/b.py": CODE, "tests/a_test.py": CODE}
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(lines, [f"ok {SOURCE_RULE}: 3 checked", f"ok {TESTS_RULE}: 1 checked"])

    def test_each_limit_plus_the_allowance_is_inclusive(self):
        body = "a = 0\n" * 100 + "x\n"
        for path, limit in (("core/a.py", 15), ("tests/a.py", 10)):
            allowed = limit * chars(body) // 100 + 500
            for extra, expected in ((0, 0), (1, 1)):
                with self.subTest(path=path, extra=extra):
                    comment = "#" + "c" * (allowed - 1 + extra)
                    self.assertEqual(self.run_in({path: body + comment + "\n"})[0], expected)

    def test_a_docstring_only_init_passes_within_the_allowance(self):
        self.assertEqual(chars(INIT), 500)
        self.assertEqual(self.run_in({"core/__init__.py": INIT})[0], 0)
        self.assertEqual(self.run_in({"core/__init__.py": INIT.replace("d", "dd", 1)})[0], 1)

    def test_a_subdirectory_does_not_count_toward_its_parent(self):
        self.assertEqual(
            self.run_in({"core/__init__.py": INIT, "core/sub/__init__.py": INIT})[0], 0
        )
        code, lines, _ = self.run_in({"core/a.py": DOC, "core/sub/b.py": CODE * 10})
        self.assertEqual(
            (code, lines[:2]),
            (1, [f"FAIL {SOURCE_RULE}: 1 of 2 over budget", over("core", chars(DOC), 0, 15)]),
        )

    def test_source_and_tests_in_one_directory_are_judged_separately(self):
        code, lines, _ = self.run_in({"core/a.py": CODE * 10, "core/a_test.py": DOC})
        self.assertEqual(
            (code, lines),
            (
                1,
                [
                    f"ok {SOURCE_RULE}: 1 checked",
                    f"FAIL {TESTS_RULE}: 1 of 1 over budget",
                    over("core", chars(DOC), 0, 10),
                    PURPOSE,
                ],
            ),
        )

    def test_one_directory_over_budget_fails_though_the_total_is_under(self):
        code, lines, _ = self.run_in({"core/a.py": CODE * 10, "lib/b.py": DOC + CODE})
        self.assertLess(chars(DOC) * 100, 15 * 11 * chars(CODE))
        self.assertEqual(
            (code, lines[:2]),
            (
                1,
                [
                    f"FAIL {SOURCE_RULE}: 1 of 2 over budget",
                    over("lib", chars(DOC), chars(CODE), 15),
                ],
            ),
        )

    def test_failure_lists_each_failing_directory_with_its_numbers_then_the_purpose_once(self):
        n, code = chars(DOC), chars(CODE)
        status, lines, err = self.run_in(
            {
                "setup.py": DOC,
                "core/a.py": CODE,
                "core/x/b.py": DOC,
                "lib/c.py": DOC + CODE,
                "tests/a_test.py": CODE,
                "tests/unit/b_test.py": DOC,
            }
        )
        self.assertEqual((status, err), (1, ""))
        self.assertEqual(
            lines,
            [
                f"FAIL {SOURCE_RULE}: 3 of 4 over budget",
                over(".", n, 0, 15),
                over("core/x", n, 0, 15),
                over("lib", n, code, 15),
                f"FAIL {TESTS_RULE}: 1 of 2 over budget",
                over("tests/unit", n, 0, 10),
                PURPOSE,
            ],
        )

    def test_untracked_files_are_ignored(self):
        self.assertEqual(self.run_in({"core/a.py": CODE}, {"core/scratch.py": DOC})[0], 0)

    def test_a_syntax_error_exits_2_naming_the_file(self):
        code, lines, err = self.run_in({"core/a.py": CODE, "core/bad.py": "def f(:\n"})
        self.assertEqual((code, lines), (2, []))
        self.assertTrue(err.startswith("comment_budget: core/bad.py: "), err)

    def test_a_git_failure_exits_2(self):
        with tempfile.TemporaryDirectory() as folder:
            out, err = io.StringIO(), io.StringIO()
            ceiling = str(Path(folder).resolve().parent)
            with (
                mock.patch.dict(os.environ, GIT_CEILING_DIRECTORIES=ceiling),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = budget.main(Path(folder))
        self.assertEqual((code, out.getvalue()), (2, ""))
        self.assertTrue(
            err.getvalue().startswith("comment_budget: git ls-files failed: "), err.getvalue()
        )
