from unittest import mock

from tests import guards
from tests.case import REPO
from tests.guard_checks import suppressions
from tests.guard_samples import PKG, Sample


class SourceSuppressions(Sample):
    def scoped(self, scope, comments, *, comment_only=False):
        lines = []
        parts = scope.split(".")
        for depth, name in enumerate(parts):
            declaration = (
                'if __name__ == "__main__":'
                if name == "__main__"
                else f"class {name}:"
                if name[0].isupper()
                else f"def {name}():"
            )
            lines.append("    " * depth + declaration)
        indent = "    " * len(parts)
        lines.extend(indent + ("" if comment_only else "pass  ") + comment for comment in comments)
        if comment_only:
            lines.append(indent + "pass")
        return "\n".join(lines) + "\n"

    def found(self, extra=None, *, replace=None):
        files = {}
        for (path, code, scope), reason in guards.NOQA_ALLOWED.items():
            comments = (
                [f"# pylint: disable={code}  # {reason}", f"# pylint: enable={code}"]
                if code == "duplicate-code"
                else [f"# noqa: {code}  # {reason}"]
            )
            files[path] = files.get(path, "") + self.scoped(scope, comments)
        files.update(extra or {})
        if replace is not None:
            files.pop(replace)
        return suppressions.source_suppressions(self.tree(files))

    def test_the_real_inventory_has_matching_comments(self):
        self.assertEqual(suppressions.source_suppressions(REPO), [])

    def test_hook_suppression_is_only_at_the_hook_run_boundary(self):
        path = "session/src/agent_pm/session/modes/tui_hook.py"
        reason = guards.NOQA_ALLOWED[path, "BLE001", "run"]
        comment = f"# noqa: BLE001  # {reason}"
        self.assertEqual(
            self.found({path: self.scoped("run", [comment]) + self.scoped("other", [comment])}),
            [f"{path}:4: suppression BLE001 in other is not allowed"],
        )

    def test_pylint_disable_requires_a_matching_enable(self):
        path = "tests/example.py"
        reason = "Fixture approval."
        with mock.patch.dict(guards.NOQA_ALLOWED, {(path, "duplicate-code", "outer"): reason}):
            self.assertEqual(
                self.found(
                    {path: self.scoped("outer", [f"# pylint: disable=duplicate-code  # {reason}"])}
                ),
                [f"{path}: suppression duplicate-code in outer has no matching enable"],
            )

    def test_unlisted_pylint_blocks_and_unmatched_enables_are_rejected(self):
        path = "tests/other.py"
        self.assertEqual(
            self.found(
                {
                    path: self.scoped(
                        "other",
                        [
                            "# pylint: disable=duplicate-code  # not approved",
                            "# pylint: enable=duplicate-code",
                        ],
                    )
                }
            ),
            [
                f"{path}:2: suppression duplicate-code in other is not allowed",
                f"{path}:3: suppression duplicate-code in other has no matching disable",
            ],
        )

    def test_pylint_enable_cannot_close_another_scope(self):
        path = "tests/example.py"
        reason = "Fixture approval."
        with mock.patch.dict(guards.NOQA_ALLOWED, {(path, "duplicate-code", "outer"): reason}):
            self.assertEqual(
                self.found(
                    {
                        path: self.scoped(
                            "outer", [f"# pylint: disable=duplicate-code  # {reason}"]
                        )
                        + self.scoped("other", ["# pylint: enable=duplicate-code"])
                    }
                ),
                [
                    f"{path}:4: suppression duplicate-code in other has no matching disable",
                    f"{path}: suppression duplicate-code in outer has no matching enable",
                ],
            )

    def test_strings_docstrings_and_unrelated_comments_are_ignored(self):
        self.assertEqual(
            self.found(
                {
                    f"{PKG}/utils/unused.py": "# noqa\n",
                    "utils/src/agent_pm/utils/strings.py": (
                        '"""Example: # noqa and # type: ignore."""\nvalue = \'# noqa: F401\'\n# Suppressions are checked elsewhere.\n'
                    ),
                }
            ),
            [],
        )

    def test_bare_noqa_empty_codes_and_missing_reasons_are_rejected(self):
        path = "utils/src/agent_pm/utils/a.py"
        for comment in ("# noqa", "# noqa:", "# noqa: F401", "# noqa: BLE001"):
            with self.subTest(comment):
                self.assertEqual(
                    self.found({path: f"value = 1  {comment}\n"}),
                    [f"{path}:1: suppression needs explicit codes and a recorded reason"],
                )

    def test_type_ignores_are_rejected_with_or_without_codes(self):
        path = "delegate/src/agent_pm/delegate/a.py"
        for comment in ("# type: ignore", "# type: ignore[assignment]", "#type:ignore"):
            with self.subTest(comment):
                self.assertEqual(
                    self.found({path: f"value = 1  {comment}\n"}),
                    [f"{path}:1: suppression needs explicit codes and a recorded reason"],
                )

    def test_every_tree_rejects_tool_pragmas(self):
        comments = (
            "# ruff: noqa",
            "# ruff: noqa: E402",
            "# flake8: noqa",
            "# noqa: E402, I001",
            "# type: ignore",
            "# pyright: ignore",
            "# pylint: disable=all",
            "# isort: skip",
        )
        paths = (
            "session/src/agent_pm/session/sample.py",
            "session/tests/unit/sample_test.py",
            "tests/unit/sample_test.py",
            ".github/sample.py",
        )
        for path in paths:
            for comment in comments:
                with self.subTest(path=path, comment=comment):
                    self.assertEqual(
                        self.found({path: f"value = 1  {comment}\n"}),
                        [f"{path}:1: suppression needs explicit codes and a recorded reason"],
                    )

    def test_e402_i001_and_isort_comments(self):
        comments = (
            "# noqa: E402",
            "# noqa: F401,E402",
            "# NOQA:I001",
            "# isort: skip_file",
            "# ruff: noqa: E402",
        )
        path = "tests/a.py"
        source = "\n".join(f"import x  {comment}" for comment in comments) + "\n"
        self.assertEqual(
            self.found({path: source}),
            [
                f"{path}:{line}: suppression needs explicit codes and a recorded reason"
                for line in range(1, 6)
            ],
        )

    def test_a_rule_in_the_wrong_file_is_rejected(self):
        path = "utils/src/agent_pm/utils/a.py"
        self.assertEqual(
            self.found({path: "value = 1  # noqa: BLE001  # Forward failures.\n"}),
            [f"{path}:1: suppression BLE001 in <module> is not allowed"],
        )

    def test_compound_codes_cannot_hide_an_unlisted_rule(self):
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        self.assertEqual(
            self.found({path: self.scoped(scope, [f"# noqa: {code}, F401  # {reason}"])}),
            [f"{path}:3: suppression F401 in {scope} is not allowed"],
        )

    def test_reason_must_match_the_inventory(self):
        (path, code, scope), _ = next(iter(guards.NOQA_ALLOWED.items()))
        self.assertEqual(
            self.found({path: self.scoped(scope, [f"# noqa: {code}  # Different reason."])}),
            [
                f"{path}:3: suppression {code} in {scope} reason differs from NOQA_ALLOWED",
                f"{path}: suppression {code} in {scope} has no matching comment",
            ],
        )

    def test_each_inventory_entry_needs_a_comment(self):
        path, code, scope = next(iter(guards.NOQA_ALLOWED))
        for files, replace in (({path: "value = 1\n"}, None), ({}, path)):
            with self.subTest(replace=replace):
                self.assertEqual(
                    self.found(files, replace=replace),
                    [f"{path}: suppression {code} in {scope} has no matching comment"],
                )

    def test_duplicate_comments_cannot_reuse_an_entry(self):
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        comment = f"# noqa: {code}  # {reason}"
        self.assertEqual(
            self.found({path: self.scoped(scope, [comment, comment])}),
            [f"{path}:4: duplicate suppression {code} in {scope}"],
        )

    def test_whitespace_case_and_comment_only_lines_are_checked(self):
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        self.assertEqual(
            self.found(
                {
                    path: self.scoped(
                        scope, [f"#  NOQA : {code.lower()} # {reason}"], comment_only=True
                    )
                }
            ),
            [],
        )
        self.assertEqual(
            self.found({path: self.scoped(scope, [f"# noqa: {code} # {reason} # type: ignore"])}),
            [
                f"{path}:3: suppression {code} in {scope} reason differs from NOQA_ALLOWED",
                f"{path}: suppression {code} in {scope} has no matching comment",
            ],
        )

    def test_same_file_and_rule_do_not_allow_another_function(self):
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        comment = f"# noqa: {code} # {reason}"
        for other in ("other", "Other._pump", "HeadlessMode.other", "HeadlessMode._pump.nested"):
            with self.subTest(other):
                source = self.scoped(scope, [comment]) + self.scoped(other, [comment])
                line = len(scope.split(".")) + len(other.split(".")) + 2
                self.assertEqual(
                    self.found({path: source}),
                    [f"{path}:{line}: suppression {code} in {other} is not allowed"],
                )

    def test_nested_functions_use_the_innermost_qualified_name(self):
        self.assertEqual(self.found(), [])
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        comment = f"# noqa: {code} # {reason}"
        source = self.scoped(scope + ".nested", [comment])
        self.assertEqual(
            self.found({path: source}),
            [
                f"{path}:4: suppression {code} in {scope}.nested is not allowed",
                f"{path}: suppression {code} in {scope} has no matching comment",
            ],
        )

    def test_async_methods_and_moved_module_comments_cannot_borrow_an_entry(self):
        (path, code, scope), reason = next(iter(guards.NOQA_ALLOWED.items()))
        source = self.scoped(scope, [f"# noqa: {code} # {reason}"])
        self.assertEqual(self.found({path: source.replace("def _pump", "async def _pump")}), [])
        self.assertEqual(
            self.found({path: f"pass  # noqa: {code} # {reason}\n"}),
            [
                f"{path}:1: suppression {code} in <module> is not allowed",
                f"{path}: suppression {code} in {scope} has no matching comment",
            ],
        )
