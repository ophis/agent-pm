"""Linear compares lower-layer enum attributes with enum members."""

import ast
import os

from tests.guard_checks.common import _files
from tests.guard_data.linear_orchestrator import ORCH_FILES


def linear_enum_comparisons(repo: str) -> list[str]:
    found = set()
    for rel, text in _files(os.path.join(repo, ORCH_FILES), ".py"):
        path = f"{ORCH_FILES}{rel}"
        for node in ast.walk(ast.parse(text, path)):
            if not isinstance(node, ast.Compare):
                continue
            values = [node.left, *node.comparators]
            for left, right in zip(values, values[1:], strict=False):
                for field, literal in ((left, right), (right, left)):
                    if (
                        isinstance(field, ast.Attribute)
                        and field.attr in {"resumable", "state", "status"}
                        and _string_literal(literal)
                    ):
                        found.add(
                            f"{path}:{node.lineno}: compare .{field.attr} with enum members, not string literals"
                        )
    return sorted(found)


def _string_literal(node):
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return any(_string_literal(value) for value in node.elts)
    if isinstance(node, ast.Dict):
        return any(_string_literal(key) for key in node.keys)
    return False
