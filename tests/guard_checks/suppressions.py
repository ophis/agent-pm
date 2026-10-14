"""Lint suppression inventory check."""

import ast
import io
import re
import tokenize

from tests.guard_checks.common import _tree_files
from tests.guards import NOQA_ALLOWED


def source_suppressions(repo: str) -> list[str]:
    """Suppressions in every code tree need an exact inventory entry, reason and one use."""
    found, seen = [], set()
    for path, text in _tree_files(repo, ".py"):
        functions = list(_suppression_functions(ast.parse(text)))
        disabled = {}
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT or not re.search(
                r"#\s*(?:(?:ruff|flake8)\s*:\s*noqa\b|noqa\b|type\s*:\s*ignore\b|(?:pyright|pylint|isort)\s*:)",
                token.string,
                re.I,
            ):
                continue
            at = f"{path}:{token.start[0]}"
            scope = next(
                (
                    name
                    for first, last, name in reversed(functions)
                    if first <= token.start[0] <= last
                ),
                "<module>",
            )
            pylint = re.fullmatch(
                r"# pylint: (disable|enable)=duplicate-code(?:\s+#\s*(.+?))?\s*",
                token.string,
            )
            if pylint:
                key = (path, "duplicate-code", scope)
                detail = f"suppression duplicate-code in {scope}"
                if pylint[1] == "enable":
                    if key not in disabled or disabled[key] != token.start[1] or pylint[2]:
                        found.append(f"{at}: {detail} has no matching disable")
                    else:
                        del disabled[key]
                elif key not in NOQA_ALLOWED:
                    found.append(f"{at}: {detail} is not allowed")
                elif pylint[2] != NOQA_ALLOWED[key]:
                    found.append(f"{at}: {detail} reason differs from NOQA_ALLOWED")
                elif key in seen:
                    found.append(f"{at}: duplicate {detail}")
                else:
                    disabled[key] = token.start[1]
                    seen.add(key)
                continue
            match = re.fullmatch(
                r"#\s*noqa\s*:\s*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)\s*#\s*(.+?)\s*",
                token.string,
                re.I,
            )
            if not match:
                found.append(f"{at}: suppression needs explicit codes and a recorded reason")
                continue
            for code in re.split(r"\s*,\s*", match[1].upper()):
                key = (path, code, scope)
                detail = f"suppression {code} in {scope}"
                if key not in NOQA_ALLOWED:
                    found.append(f"{at}: {detail} is not allowed")
                elif match[2] != NOQA_ALLOWED[key]:
                    found.append(f"{at}: {detail} reason differs from NOQA_ALLOWED")
                elif key in seen:
                    found.append(f"{at}: duplicate {detail}")
                else:
                    seen.add(key)
        found.extend(
            f"{path}: suppression {code} in {scope} has no matching enable"
            for _, code, scope in disabled
        )
    found.extend(
        f"{path}: suppression {code} in {scope} has no matching comment"
        for path, code, scope in sorted(NOQA_ALLOWED.keys() - seen)
    )
    return found


def _suppression_functions(tree, parents=()):
    for node in ast.iter_child_nodes(tree):
        scope = parents
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (*parents, node.name)
            if not isinstance(node, ast.ClassDef):
                yield node.lineno, node.end_lineno, ".".join(scope)
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.Eq)
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "__main__"
        ):
            scope = (*parents, "__main__")
            yield node.lineno, node.end_lineno, ".".join(scope)
        yield from _suppression_functions(node, scope)
