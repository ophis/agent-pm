"""Import rule and import side-effect checks."""

import ast
import fnmatch
import os
import sys
import tomllib

from tests.guard_checks.common import (
    _at_import,
    _bindings,
    _boundary_bindings,
    _boundary_name,
    _dotted,
    _files,
    _private,
    _tree_files,
    _within,
)
from tests.guards import (
    DELEGATE_SESSION_FORWARDING_ALLOWED,
    IMPORT_BOOTSTRAPS,
    ORCH_PKG,
    ORCH_PROJECT,
    ORCH_SESSION_CONTRACT,
    ORCH_SESSION_IMPORTS,
    ORCH_SRC,
    PKG,
    PROJECTS,
    SESSION,
    STANDALONE,
)
from tests.import_policy import violation

READS = {"copy", "count", "get", "index", "items", "keys", "values"}


def _spellings(nodes, dotted):
    module, attr = dotted.split(".")
    found = {dotted}
    for node in nodes:
        if isinstance(node, ast.Import):
            found.update(f"{a.asname}.{attr}" for a in node.names if a.name == module and a.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == module and not node.level:
            found.update(a.asname or a.name for a in node.names if a.name == attr)
    return found


def _is(node, spellings):
    return isinstance(node, ast.Attribute | ast.Name) and ast.unparse(node) in spellings


def _changes(node, spellings):
    if isinstance(node, ast.AugAssign):
        return _is(node.target, spellings)
    if isinstance(getattr(node, "ctx", None), ast.Store | ast.Del):
        target = node.value if isinstance(node, ast.Subscript) else node
        return not isinstance(node, ast.Name) and _is(target, spellings)
    if not isinstance(node, ast.Call):
        return False
    func, args = node.func, node.args
    if isinstance(func, ast.Attribute) and _is(func.value, spellings):
        return func.attr not in READS
    return (
        "patch" in ast.unparse(func).split(".")
        and bool(args)
        and (
            _is(args[0], spellings)
            or (isinstance(args[0], ast.Constant) and args[0].value in spellings)
            or (
                len(args) > 1
                and isinstance(args[1], ast.Constant)
                and f"{ast.unparse(args[0])}.{args[1].value}" in spellings
            )
        )
    )


def delegate_session_forwarding(repo: str) -> list[str]:
    """FR-34: Delegate uses Session and RunLog, never deprecated session wrappers."""
    base = "delegate/src/agent_pm/delegate/"
    wrappers = {"Driver", "Handle", "close", "note", "session_record", "sessions"}
    found, seen = set(), set()
    for rel, text in _files(os.path.join(repo, base), ".py"):
        tree = ast.parse(text, rel)
        bound = _boundary_bindings(tree)
        for node in ast.walk(tree):
            names = (
                [f"{node.module}.{alias.name}" for alias in node.names]
                if isinstance(node, ast.ImportFrom) and node.module and not node.level
                else [_boundary_name(node, bound)]
                if isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load)
                else []
            )
            for name in names:
                for prefix in ("agent_pm.session.", "agent_pm.session.engine.forwarding."):
                    if name.startswith(prefix) and name[len(prefix) :].split(".")[0] in wrappers:
                        key = base + rel, prefix + name[len(prefix) :].split(".")[0]
                        if key in DELEGATE_SESSION_FORWARDING_ALLOWED:
                            seen.add(key)
                        else:
                            found.add(
                                f"{base}{rel}:{node.lineno}: session forwarding {name} (FR-34)"
                            )
    found.update(
        f"{rel}: {name}: allowlisted but no forwarding reference (FR-34)"
        for rel, name in DELEGATE_SESSION_FORWARDING_ALLOWED.keys() - seen
    )
    return sorted(found)


def orchestrator_session_imports(repo: str) -> list[str]:
    """Orchestration reaches session only through Delegate."""
    found = []
    config = os.path.join(repo, "pyproject.toml")
    if os.path.isfile(config):
        with open(config, "rb") as file:
            contracts = tomllib.load(file)["tool"]["importlinter"]["contracts"]
        listed = next(
            (c.get("ignore_imports") for c in contracts if c["name"] == ORCH_SESSION_CONTRACT), None
        )
        pinned = [f"{m} -> {PKG}.{SESSION}" for m in sorted(ORCH_SESSION_IMPORTS)]
        if sorted(listed or ()) != pinned:
            found.append(
                f"pyproject.toml: {ORCH_SESSION_CONTRACT!r} must ignore exactly ORCH_SESSION_IMPORTS"
            )
    session = f"{PKG}.{SESSION}."
    for rel, text in _files(os.path.join(repo, ORCH_PROJECT, ORCH_SRC), ".py"):
        module = f"{ORCH_PKG}.{rel.removesuffix('.py').replace('/', '.')}".removesuffix(".__init__")
        tree = ast.parse(text)
        bound = _bindings(tree)
        names = {
            dotted.removeprefix(session).split(".")[0]
            for dotted in (
                *bound.values(),
                *(_dotted(n, bound) for n in ast.walk(tree) if isinstance(n, ast.Attribute)),
            )
            if dotted.startswith(session)
        }
        found += [
            f"{ORCH_PROJECT}/{ORCH_SRC}{rel}: {module} imports {name} from {PKG}.{SESSION}; "
            "go through agent_pm.delegate"
            for name in sorted(names - ORCH_SESSION_IMPORTS.get(module, set()))
        ]
    return found


def literal_imports(repo: str) -> list[str]:
    """Literal dynamic imports obey the same contracts as normal imports."""
    config = os.path.join(repo, "pyproject.toml")
    if not os.path.isfile(config):
        return []
    with open(config, "rb") as file:
        contracts = tomllib.load(file)["tool"]["importlinter"]["contracts"]
    found = set()
    for project in PROJECTS:
        for rel, text in _files(os.path.join(repo, project, "src"), ".py"):
            module = rel.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            tree = ast.parse(text)
            bound = _bindings(tree)
            for call in ast.walk(tree):
                if (
                    not isinstance(call, ast.Call)
                    or not call.args
                    or _dotted(call.func, bound)
                    not in {"importlib.import_module", "__import__", "builtins.__import__"}
                ):
                    continue
                if any(isinstance(node, ast.Name) for node in ast.walk(call.args[0])):
                    continue
                target = _boundary_name(call.args[0], {})
                if target and any(
                    _literal_violation(module, target, contract) for contract in contracts
                ):
                    found.add(f"{project}/src/{rel}:{call.lineno}: imports {target}")
    return sorted(found)


def _literal_violation(source, target, contract):
    if contract["type"] == "units":
        return violation(source, target, contract)
    if contract["type"] == "forbidden":
        return any(_within(source, module) for module in contract["source_modules"]) and any(
            fnmatch.fnmatchcase(target, module) if "*" in module else _within(target, module)
            for module in contract["forbidden_modules"]
        )
    if contract["type"] == "layers":
        own = next(
            (i for i, layer in enumerate(contract["layers"]) if _within(source, layer)), None
        )
        lower = next(
            (i for i, layer in enumerate(contract["layers"]) if _within(target, layer)), None
        )
        return own is not None and lower is not None and lower < own
    raise ValueError(f"unsupported import contract type: {contract['type']!r}")


def standalone_imports(repo: str) -> list[str]:
    return _stdlib_imports(repo, STANDALONE)


def test_gate_imports(repo: str) -> list[str]:
    return _stdlib_imports(repo, {".github/isolation.py", "utils/tests/sandbox.py"})


def _stdlib_imports(repo, paths):
    found = []
    standard = sys.stdlib_module_names | {"__future__", "builtins"}
    for rel in sorted(paths):
        path = os.path.join(repo, rel)
        if not os.path.isfile(path):
            found.append(f"{rel}: missing standalone script")
            continue
        with open(path, encoding="utf-8") as file:
            tree = ast.parse(file.read(), rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = ["." * node.level + (node.module or "")]
            else:
                continue
            found.extend(
                f"{rel}:{node.lineno}: non-stdlib import {name}"
                for name in names
                if name.split(".")[0] not in standard
            )
    return sorted(found)


def old_core(repo: str) -> list[str]:
    return sorted(
        f"{rel}: imports core"
        for rel, text in _tree_files(repo, ".py")
        for node in ast.walk(ast.parse(text, rel))
        if isinstance(node, ast.Import)
        and any(a.name.split(".")[0] == "core" for a in node.names)
        or isinstance(node, ast.ImportFrom)
        and (node.module or "").split(".")[0] == "core"
        or isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.split(".")[0] == "core"
        and ast.unparse(node.func).split(".")[-1] in {"__import__", "import_module"}
    )


PUBLIC_UNDERSCORE = frozenset({"os._exit"})


def private_imports(root: str) -> list[str]:
    """Ruff 0.16.9 PLC2701 is preview; this guard supplies stable private-import checks."""
    found = set()
    for rel, text in _files(root, ".py"):
        tree = ast.parse(text, rel)
        imported = set()
        bound = _bindings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(a.asname or a.name for a in node.names)
                module = "." * node.level + (f"{node.module}." if node.module else "")
                found.update(
                    f"{rel}:{node.lineno}: private {module}{a.name}"
                    for a in node.names
                    if _private(a.name) and f"{module}{a.name}" not in PUBLIC_UNDERSCORE
                )
            elif isinstance(node, ast.Import):
                imported.update(a.asname or a.name.split(".")[0] for a in node.names)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and _private(node.attr)
                and _dotted(node, bound) not in PUBLIC_UNDERSCORE
            ):
                chain, base = [node.attr], node.value
                while isinstance(base, ast.Attribute):
                    chain.append(base.attr)
                    base = base.value
                if isinstance(base, ast.Name) and base.id in imported:
                    found.add(
                        f"{rel}:{node.lineno}: private {'.'.join([base.id, *reversed(chain)])}"
                    )
    return sorted(found)


def import_hygiene(repo: str) -> list[str]:
    found = set()
    for rel, text in _tree_files(repo, ".py"):
        tree = ast.parse(text, rel)
        nodes = list(ast.walk(tree))
        path, environ, putenv = (
            _spellings(nodes, n) for n in ("sys.path", "os.environ", "os.putenv")
        )
        bootstrap = _path_bootstrap(rel, tree)
        found.update(
            f"{rel}:{n.lineno}: sys.path mutation"
            for n in nodes
            if _changes(n, path) and n is not bootstrap
        )
        found.update(
            f"{rel}:{n.lineno}: module-level environment write"
            for n in _at_import(tree)
            if _changes(n, environ) or isinstance(n, ast.Call) and _is(n.func, putenv)
        )
    return sorted(found)


def _path_bootstrap(rel, tree):
    name = IMPORT_BOOTSTRAPS.get(rel)
    if name is None:
        return None
    assignment = ast.parse(
        "sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]"
    ).body[0]
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef) or function.name != name:
            continue
        for statement in function.body:
            if isinstance(statement, ast.Assign) and ast.dump(statement) == ast.dump(assignment):
                return statement.targets[0]
    return None
