"""Python -c programs are literal text keeping the public import boundary of command callers."""

import ast
import copy
import hashlib
import os
import re
import sys

from tests.guard_checks.common import _bindings, _dotted, _private, _tree_files
from tests.guard_data import PYTHON_CODE_ALLOWED
from tests.guards import PROJECTS

IMPORTERS = {"__import__", "builtins.__import__", "importlib.import_module", "runpy.run_module"}
ATTRIBUTE_CALLS = {"getattr", "setattr", "delattr", "hasattr"}
RUNTIME = object()


def inline_python(repo: str, *, allowed=None) -> list[str]:
    allowed = PYTHON_CODE_ALLOWED if allowed is None else allowed
    found, used = [], set()
    for path, text in _tree_files(repo, ".py"):
        for line, code, error in _programs(ast.parse(text)):
            errors = [error] if error else _violations(code)
            if not errors:
                continue
            key = code is not None and (path, hashlib.sha256(code.encode()).hexdigest())
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


def _programs(tree, inherited=None):
    """(line, text, error) per -c program: each literal value, else the expression with its
    same-file values substituted, so editing one makes its exception stale; a program reading a
    parameter, import or loop target gets text None: no exception can cover all its values.
    """
    body = tree.body if isinstance(tree.body, list) else [tree.body]
    nodes = list(_scope(body))
    assigned = {}
    for node in nodes:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                assigned[target] = node.value
    function = isinstance(tree, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
    local = {}
    for node in [*(ast.walk(tree.args) if function else ()), *nodes]:
        if isinstance(node, ast.arg):
            local.setdefault(node.arg, []).append(RUNTIME)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if (value := assigned.get(node, RUNTIME)) is not None:
                local.setdefault(node.id, []).append(value)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                local.setdefault(alias.asname or alias.name.split(".")[0], []).append(RUNTIME)
    bindings = {**(inherited or {}), **local}
    for node in nodes:
        if isinstance(node, ast.List | ast.Tuple) and node.elts and _python(node.elts[0], bindings):
            for before, after in zip(node.elts, node.elts[1:], strict=False):
                if _strings(before, bindings) != {"-c"}:
                    continue
                if codes := _strings(after, bindings):
                    yield from ((after.lineno, code, None) for code in sorted(codes))
                    continue
                text, runtime = _substitute(after, bindings)
                hint = " (run-time value; build the argv from literals at the call site)"
                yield (
                    after.lineno,
                    None if runtime else text,
                    f"not a literal: {ast.unparse(after)}{hint if runtime else ''}",
                )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            # Class bindings are not visible inside its methods.
            yield from _programs(node, inherited if isinstance(tree, ast.ClassDef) else bindings)


def _strings(node, bindings, seen=frozenset()):
    """Every text node can take, or the empty set when any of them is not literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id not in seen and bindings.get(node.id):
        values = [
            _strings(value, bindings, seen | {node.id}) if isinstance(value, ast.AST) else set()
            for value in bindings[node.id]
        ]
        return set().union(*values) if all(values) else set()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return {
            left + right
            for left in _strings(node.left, bindings, seen)
            for right in _strings(node.right, bindings, seen)
        }
    return set()


def _substitute(node, bindings):
    runtime = False

    class Substitute(ast.NodeTransformer):
        seen = set()

        def visit_Name(self, name):
            nonlocal runtime
            values = bindings.get(name.id, ())
            runtime = runtime or RUNTIME in values
            if name.id in self.seen:
                return name
            self.seen.add(name.id)
            found = [self.visit(copy.deepcopy(value)) for value in values if value is not RUNTIME]
            self.seen.discard(name.id)
            return found[0] if len(values) == 1 and found else name

    return ast.unparse(Substitute().visit(copy.deepcopy(node))), runtime


def _python(node, bindings, seen=frozenset()):
    if isinstance(node, ast.Starred):
        return _python(node.value, bindings, seen)
    if isinstance(node, ast.Name) and node.id not in seen:
        if node.id in {"PY", "PYTHON", "PYTHON3", "python"}:
            return True
        return any(
            _python(value, bindings, seen | {node.id})
            for value in bindings.get(node.id, ())
            if isinstance(value, ast.AST)
        )
    if isinstance(node, ast.List | ast.Tuple) and node.elts:
        return _python(node.elts[0], bindings, seen)
    if any(_dotted(part, {}) == "sys.executable" for part in ast.walk(node)):
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


def _command(module):
    parts = module.split(".")
    return (
        len(parts) == 3
        and parts[0] == "agent_pm"
        and parts[1] in PROJECTS.values()
        and parts[2] == "cli"
    )


def _stdlib_names(tree):
    """Names bound only by imports of standard library modules."""
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                bound.setdefault(alias.asname or top, []).append(top in sys.stdlib_module_names)
        elif isinstance(node, ast.ImportFrom):
            stdlib = not node.level and (node.module or "").split(".")[0] in sys.stdlib_module_names
            for alias in node.names:
                bound.setdefault(alias.asname or alias.name, []).append(stdlib)
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            bound.setdefault(node.id, []).append(False)
        elif isinstance(node, ast.arg):
            bound.setdefault(node.arg, []).append(False)
        elif (
            isinstance(
                node, ast.ExceptHandler | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            )
            and node.name
        ):
            bound.setdefault(node.name, []).append(False)
    return {name for name, stdlib in bound.items() if all(stdlib)}


def _violations(code):
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError) as error:
        return [f"invalid code: {error}"]
    bound = _bindings(tree)
    found, agent = set(), False
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = ["." * node.level + (node.module or "")]
            if (node.module or "").startswith("agent_pm."):
                found.update(
                    f"private import {alias.name}" for alias in node.names if _private(alias.name)
                )
        elif isinstance(node, ast.Call) and (call := _dotted(node.func, bound)) in {
            *IMPORTERS,
            "runpy.run_path",
        }:
            names = _strings(node.args[0], {}) if node.args and call in IMPORTERS else set()
            if not names:
                found.add(f"dynamic import: {ast.unparse(node)}")
            modules = [
                name for name in names if not (call == "runpy.run_module" and _command(name))
            ]
            agent = agent or any(name.split(".")[0] == "agent_pm" for name in names)
        for module in modules:
            agent = agent or module.split(".")[0] == "agent_pm"
            if module.startswith(".") or not _public(module):
                found.add(f"import {module}")
    if agent:
        stdlib = _stdlib_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and _private(node.attr):
                base, names = node.value, {node.attr}
            elif (
                isinstance(node, ast.Call)
                and _dotted(node.func, bound) in ATTRIBUTE_CALLS
                and len(node.args) > 1
            ):
                base, names = node.args[0], {n for n in _strings(node.args[1], {}) if _private(n)}
            else:
                continue
            if not (isinstance(base, ast.Name) and base.id in stdlib):
                found.update(f"private attribute {name}" for name in names)
    return sorted(found)
