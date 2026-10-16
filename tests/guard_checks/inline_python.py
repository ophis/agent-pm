"""Literal Python -c programs keep the same public import boundary as command callers."""

import ast
import hashlib
import os
import re
import sys

from tests.guard_checks.common import _tree_files
from tests.guard_data import PYTHON_CODE_ALLOWED
from tests.guards import PROJECTS


def inline_python(repo: str, *, allowed=None) -> list[str]:
    allowed = PYTHON_CODE_ALLOWED if allowed is None else allowed
    found, used = [], set()
    files = {path: ast.parse(text) for path, text in _tree_files(repo, ".py")}
    modules = {_module(path): tree for path, tree in files.items()}
    for path, tree in files.items():
        for line, code in _programs(tree, modules=modules):
            errors = _violations(code)
            if not errors:
                continue
            key = path, hashlib.sha256(code.encode()).hexdigest()
            if key in allowed:
                used.add(key)
            else:
                found.append(f"{path}:{line}: Python -c: {', '.join(errors)}")
    found.extend(
        f"{path}: stale Python -c exception {digest}" for path, digest in allowed.keys() - used
    )
    found.extend(
        f"{path}: Python -c exception needs one-line reason"
        for (path, _), reason in allowed.items()
        if not isinstance(reason, str) or not reason.strip() or "\n" in reason
    )
    return sorted(set(found))


def _scope(nodes):
    for node in nodes:
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            yield from _scope(ast.iter_child_nodes(node))


def _module(path):
    for project in PROJECTS:
        path = path.removeprefix(f"{project}/src/")
    path = path.removeprefix("linear-orchestrator/tests/").removeprefix(".github/")
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def _imported(module, name, modules, seen=frozenset()):
    key = module, name
    if module not in modules or key in seen:
        return set()
    nodes = list(_scope(modules[module].body))
    values = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            values.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound == name:
                    return _imported(node.module, alias.name, modules, seen | {key})
    return _strings(ast.Name(id=name), values)


def _programs(tree, inherited=None, *, modules=None):
    nodes = list(_scope(tree.body))
    local = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local.setdefault(target.id, []).append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            local.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            for alias in node.names:
                values = _imported(node.module, alias.name, modules or {})
                if values:
                    local[alias.asname or alias.name] = [
                        ast.Constant(value=value) for value in values
                    ]
    bindings = {**(inherited or {}), **local}
    for node in nodes:
        if isinstance(node, ast.List | ast.Tuple) and node.elts and _python(node.elts[0], bindings):
            for before, after in zip(node.elts, node.elts[1:], strict=False):
                if _strings(before, bindings) == {"-c"}:
                    for code in _strings(after, bindings):
                        yield after.lineno, code
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield from _programs(node, bindings, modules=modules)


def _strings(node, bindings, seen=frozenset()):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id not in seen:
        return {
            text
            for value in bindings.get(node.id, ())
            for text in _strings(value, bindings, seen | {node.id})
        }
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return {
            left + right
            for left in _strings(node.left, bindings, seen)
            for right in _strings(node.right, bindings, seen)
        }
    return set()


def _python(node, bindings, seen=frozenset()):
    if isinstance(node, ast.Starred):
        return _python(node.value, bindings, seen)
    if isinstance(node, ast.Name) and node.id not in seen:
        if node.id in {"PY", "PYTHON", "PYTHON3", "python"}:
            return True
        return any(
            _python(value, bindings, seen | {node.id}) for value in bindings.get(node.id, ())
        )
    if isinstance(node, ast.List | ast.Tuple) and node.elts:
        return _python(node.elts[0], bindings, seen)
    if any(
        isinstance(part, ast.Attribute) and part.attr == "executable" for part in ast.walk(node)
    ):
        return True
    return any(
        re.fullmatch(r"python(?:3(?:\.\d+)?)?", os.path.basename(word))
        for word in _strings(node, bindings)
    )


def _public(module):
    parts = module.split(".")
    return (
        parts[0] in sys.stdlib_module_names
        or len(parts) == 2
        and parts[0] == "agent_pm"
        and parts[1] in PROJECTS.values()
    )


def _agent(node, names):
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return _agent(node.value, names)
    if isinstance(node, ast.Call):
        return _agent(node.func, names)
    return False


def _violations(code):
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError) as error:
        return [f"invalid code: {error}"]
    found, names = set(), set()
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _public(alias.name):
                    found.add(f"import {alias.name}")
                if alias.name.startswith("agent_pm."):
                    names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level or not _public(node.module or ""):
                found.add(f"import {'.' * node.level}{node.module or ''}")
            if (node.module or "").startswith("agent_pm."):
                names.update(alias.asname or alias.name for alias in node.names)
                found.update(
                    f"private import {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("_")
                )
    for _ in range(len(nodes)):
        added = {
            target.id
            for node in nodes
            if isinstance(node, ast.Assign | ast.AnnAssign) and _agent(node.value, names)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        } - names
        if not added:
            break
        names.update(added)
    for node in nodes:
        if (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("_")
            and _agent(node.value, names)
        ):
            found.add(f"private attribute {node.attr}")
        elif isinstance(node, ast.Call):
            call = ast.unparse(node.func)
            if (
                call in {"getattr", "setattr", "delattr"}
                and len(node.args) > 1
                and _agent(node.args[0], names)
            ):
                for name in _strings(node.args[1], {}):
                    if name.startswith("_"):
                        found.add(f"private attribute {name}")
            if call in {"__import__", "importlib.import_module"} and node.args:
                for module in _strings(node.args[0], {}):
                    if not _public(module):
                        found.add(f"import {module}")
    return sorted(found)
