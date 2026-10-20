"""The orchestrator's own rules: enum comparisons, and engine/ kept free of trackers and Linear."""

import ast
import os
import tomllib

from tests.guard_checks.common import _files
from tests.guard_data.orchestrator import ENGINE_CONTRACT, ENGINE_LINEAR, ORCH_FILES, ORCH_PKG


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


def engine_tracker_contract(repo: str) -> list[str]:
    config = os.path.join(repo, "pyproject.toml")
    if not os.path.isfile(config):
        return []
    with open(config, "rb") as file:
        contracts = tomllib.load(file)["tool"]["importlinter"]["contracts"]
    found = next((c for c in contracts if c["name"] == ENGINE_CONTRACT), None)
    if found is None:
        return [f"pyproject.toml: no contract named {ENGINE_CONTRACT!r}"]
    errors = []
    if found.get("source_modules") != [f"{ORCH_PKG}.engine"]:
        errors.append(
            f"pyproject.toml: {ENGINE_CONTRACT!r} must have source_modules [{ORCH_PKG}.engine]"
        )
    if f"{ORCH_PKG}.trackers" not in found.get("forbidden_modules", ()):
        errors.append(f"pyproject.toml: {ENGINE_CONTRACT!r} must forbid {ORCH_PKG}.trackers")
    if found.get("allow_indirect_imports"):
        errors.append(f"pyproject.toml: {ENGINE_CONTRACT!r} must not allow indirect imports")
    if found.get("ignore_imports"):
        errors.append(f"pyproject.toml: {ENGINE_CONTRACT!r} must ignore no import")
    return errors


def engine_names_no_linear(repo: str) -> list[str]:
    found = []
    for rel, text in _files(os.path.join(repo, ORCH_FILES, "engine"), (".py", ".toml")):
        for n, line in enumerate(text.splitlines(), 1):
            if m := ENGINE_LINEAR.search(line):
                found.append(f"{ORCH_FILES}engine/{rel}:{n}: engine names a Linear concept: {m[0]}")
    return found
