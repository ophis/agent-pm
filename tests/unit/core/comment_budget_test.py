import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

PATH = Path(__file__).resolve().parents[3] / ".github" / "comment_budget.py"
spec = importlib.util.spec_from_file_location("comment_budget", PATH)
budget = importlib.util.module_from_spec(spec)
spec.loader.exec_module(budget)

CODE = "def f(x):\n    return [x * 2 for x in range(100) if x % 3]\n" * 20
DOC = '"""' + "A long docstring that only explains what the code already says. " * 20 + '"""\n'
PURPOSE = ("The comment budget keeps comments short: meet it by cutting comments or docstrings, "
           "never by adding code to lower the ratio.")


def chars(text):
    return len("".join(text.split()))


class Measure(unittest.TestCase):
    def test_comments_count_with_their_hash_but_a_shebang_does_not(self):
        self.assertEqual(budget.measure("x = 1  # a note\n"), (chars("# a note"), 0, chars("x = 1")))
        self.assertEqual(budget.measure("#!/usr/bin/env python3\nx = 1\n"),
                         (0, 0, chars("#!/usr/bin/env python3 x = 1")))
        self.assertEqual(budget.measure("x = 1\n#! not a shebang\n"), (chars("#! not a shebang"), 0, chars("x = 1")))

    def test_docstrings_and_other_bare_strings_count(self):
        strings = ['"""Module."""', "'''Class\n    over two lines.'''", '"Function."', '"inside an if"']
        text = (f"{strings[0]}\nclass A:\n    {strings[1]}\n    def f(self):\n        {strings[2]}\n"
                f"        if self:\n            {strings[3]}\n        return 1\n")
        comment, docstring, code = budget.measure(text)
        self.assertEqual((comment, docstring), (0, sum(map(chars, strings))))
        self.assertEqual(code, chars(text) - docstring)

    def test_ordinary_strings_are_code(self):
        text = ('x = "an error message"\nprint("help text", x)\ny = f"{x} more"\nDOC = """assigned, not bare"""\n'
                'def f():\n    return "a value"\n')
        self.assertEqual(budget.measure(text), (0, 0, chars(text)))

    def test_code_is_every_other_character(self):
        text = '"""Doc."""\nx = "s"  # note\n'
        comment, docstring, code = budget.measure(text)
        self.assertEqual((comment, docstring, comment + docstring + code),
                         (chars("# note"), chars('"""Doc."""'), chars(text)))


class Rules(unittest.TestCase):
    def test_paths_pick_their_rule(self):
        for path in ("tests/a.py", "x/tests/b.py", "new/tests/c.py", "core/d_test.py"):
            with self.subTest(path=path):
                self.assertIs(budget.rule(path), budget.TESTS)
        for path in ("core/src/e.py", ".github/comment_budget.py", "contests/f.py", "tests.py"):
            with self.subTest(path=path):
                self.assertIs(budget.rule(path), budget.SOURCE)


class Main(unittest.TestCase):
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

    def test_under_budget_passes(self):
        code, lines, err = self.run_in({"core/a.py": CODE, "tests/a_test.py": CODE})
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([line.split(":")[0] for line in lines], ["ok source", "ok tests"])
        self.assertNotIn(PURPOSE, lines)

    def test_each_limit_is_inclusive(self):
        body = "a = 0\n" * 100
        for path, limit in (("core/a.py", 15), ("tests/a.py", 10)):
            for extra, expected in ((0, 0), (1, 1)):
                with self.subTest(path=path, extra=extra):
                    comment = "#" + "c" * (3 * limit - 1 + extra)
                    self.assertEqual(self.run_in({path: body + comment + "\n"})[0], expected)

    def test_a_large_docstring_fails_its_rule_and_lists_its_file(self):
        for path, failing, limit, passing in (("core/scratch.py", budget.SOURCE, 15, "tests"),
                                              ("tests/scratch.py", budget.TESTS, 10, "source")):
            with self.subTest(path=path):
                code, lines, err = self.run_in({"core/a.py": CODE, "tests/a_test.py": CODE, path: DOC + CODE})
                n, total = chars(DOC), 2 * chars(CODE)
                fail = (f"FAIL {failing.name}: {n / total:.2%} > {limit}% ({n} comment+docstring / {total} "
                        f"code characters; {n - limit * total // 100} over budget)")
                self.assertEqual((code, err), (1, ""))
                self.assertIn(fail, lines)
                self.assertEqual(lines[lines.index(fail) + 1], f"  {n / chars(CODE):.2%} {path}")
                self.assertTrue(any(line.startswith(f"ok {passing}:") for line in lines))
                self.assertEqual((lines.count(PURPOSE), lines[-1]), (1, PURPOSE))

    def test_both_rules_failing_state_the_purpose_once_after_the_rule_lines(self):
        code, lines, _ = self.run_in({"core/scratch.py": DOC, "tests/scratch.py": DOC})
        self.assertEqual(code, 1)
        self.assertEqual([line.split(":")[0] for line in lines[:-1]],
                         ["FAIL source", "  inf core/scratch.py", "FAIL tests", "  inf tests/scratch.py"])
        self.assertEqual(lines[-1], PURPOSE)

    def test_untracked_files_are_ignored(self):
        self.assertEqual(self.run_in({"core/a.py": CODE}, {"core/scratch.py": DOC})[0], 0)

    def test_a_docstring_only_file_ranks_first(self):
        code, lines, _ = self.run_in({"core/a.py": CODE, "core/b.py": DOC + CODE, "core/c.py": DOC})
        self.assertEqual(code, 1)
        self.assertEqual(lines[1:3], ["  inf core/c.py", f"  {chars(DOC) / chars(CODE):.2%} core/b.py"])
        self.assertTrue(lines[3].startswith("ok tests:"))

    def test_at_most_ten_files_are_listed(self):
        code, lines, _ = self.run_in({f"core/{name}.py": DOC for name in "abcdefghijkl"})
        self.assertEqual(code, 1)
        self.assertEqual(lines[1:11], [f"  inf core/{name}.py" for name in "abcdefghij"])
        self.assertTrue(lines[11].startswith("ok tests:"))
        self.assertEqual(lines[12:], [PURPOSE])

    def test_a_syntax_error_exits_2_naming_the_file(self):
        code, lines, err = self.run_in({"core/a.py": CODE, "core/bad.py": "def f(:\n"})
        self.assertEqual((code, lines), (2, []))
        self.assertTrue(err.startswith("comment_budget: core/bad.py: "), err)

    def test_a_git_failure_exits_2(self):
        with tempfile.TemporaryDirectory() as folder:
            out, err = io.StringIO(), io.StringIO()
            ceiling = str(Path(folder).resolve().parent)
            with mock.patch.dict(os.environ, GIT_CEILING_DIRECTORIES=ceiling), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = budget.main(Path(folder))
        self.assertEqual((code, out.getvalue()), (2, ""))
        self.assertTrue(err.getvalue().startswith("comment_budget: git ls-files failed: "), err.getvalue())
