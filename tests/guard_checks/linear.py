"""The Linear layer's own rules: enum comparisons, and flow kept free of Linear."""

import ast
import os
import tomllib

from tests.guard_checks.common import _files, _strings
from tests.guard_data.linear_orchestrator import (
    FLOW_BINDING,
    FLOW_CONTRACT,
    FLOW_LINEAR,
    GRAPHQL,
    ORCH_FILES,
    ORCH_NAMES,
    ORCH_PKG,
)


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


def flow_binding_contract(repo: str) -> list[str]:
    config = os.path.join(repo, "pyproject.toml")
    if not os.path.isfile(config):
        return []
    with open(config, "rb") as file:
        contracts = tomllib.load(file)["tool"]["importlinter"]["contracts"]
    found = next((c for c in contracts if c["name"] == FLOW_CONTRACT), None)
    if found is None:
        return [f"pyproject.toml: no contract named {FLOW_CONTRACT!r}"]
    forbidden = set(found.get("forbidden_modules", ()))
    errors = [
        f"pyproject.toml: {FLOW_CONTRACT!r} must forbid {module}"
        for module in sorted(_binding(os.path.join(repo, ORCH_FILES, "engine")) - forbidden)
    ]
    if found.get("allow_indirect_imports"):
        errors.append(f"pyproject.toml: {FLOW_CONTRACT!r} must not allow indirect imports")
    return errors


def _binding(engine):
    names = set()
    for rel, text in _files(engine, ".py"):
        name = rel.removesuffix(".py")
        if (
            name in FLOW_BINDING
            or name == "issue"
            or name.startswith("issue_")
            or any(GRAPHQL.search(v) for _, v in _strings(ast.parse(text)))
        ):
            names.add(f"{ORCH_PKG}.engine.{name.replace('/', '.')}")
    return names


def flow_names_no_linear(repo: str) -> list[str]:
    found = []
    for rel, text in _files(os.path.join(repo, ORCH_FILES, "flow"), (".py", ".toml")):
        for n, line in enumerate(text.splitlines(), 1):
            if m := FLOW_LINEAR.search(ORCH_NAMES.sub("", line)):
                found.append(f"{ORCH_FILES}flow/{rel}:{n}: flow names a Linear concept: {m[0]}")
    return found
