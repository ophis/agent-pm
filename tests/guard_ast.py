"""AST and file traversal helpers for repository design checks."""

import ast
import io
import os
import tokenize

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


def _at_import(node):
    # a decorator runs at import, but a patch.dict one writes only when its test runs
    for child in ast.iter_child_nodes(node):
        if child not in getattr(node, "decorator_list", ()) and not isinstance(
            child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
        ):
            yield child
            yield from _at_import(child)


def _bindings(tree):
    bound = {}
    for node in ast.walk(tree):
        for a in node.names if isinstance(node, (ast.Import, ast.ImportFrom)) else ():
            if isinstance(node, ast.Import):
                bound[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0]
                )
            elif node.module and not node.level:
                bound[a.asname or a.name] = f"{node.module}.{a.name}"
    return bound


def _dotted(node, bound):
    chain = []
    while isinstance(node, ast.Attribute):
        chain.append(node.attr)
        node = node.value
    base = [bound.get(node.id, node.id)] if isinstance(node, ast.Name) else []
    return ".".join([*base, *reversed(chain)])


def _private(name):
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _within(name, unit):
    return name == unit or name.startswith(f"{unit}.")


def _files(root, suffixes, skip=None):
    for top, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            rel = os.path.relpath(os.path.join(top, name), root).replace(os.sep, "/")
            if name.endswith(suffixes) and not (skip and rel.startswith(skip)):
                with open(os.path.join(top, name), encoding="utf-8") as f:
                    yield rel, f.read()


def _strings(tree):
    return (
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


def _comment_blocks(text):
    lines = text.splitlines()
    blocks, current = [], []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT or lines[token.start[0] - 1][: token.start[1]].strip():
            continue
        line, indent = token.start
        if current and (line != current[-1][0] + 1 or indent != current[-1][2]):
            blocks.append(current)
            current = []
        current.append((line, token.string[1:].strip(), indent))
    blocks.append(current)
    return blocks


def _doc_blocks(text):
    lines, blocks = text.splitlines(), []
    for node in ast.walk(ast.parse(text)):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body or not isinstance(body[0], ast.Expr):
            continue
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            entries = [
                (line, lines[line - 1].strip(), 0)
                for line in range(value.lineno, value.end_lineno + 1)
            ]
            entries[0] = (entries[0][0], entries[0][1].lstrip("rRuU")[3:], 0)
            entries[-1] = (entries[-1][0], entries[-1][1][:-3], 0)
            blocks.append(entries)
    return blocks


def _boundary_name(node, aliases):
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_boundary_name(node.value, aliases)}.{node.attr}"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _boundary_name(node.left, aliases) + _boundary_name(node.right, aliases)
    return ""


def _boundary_bindings(tree):
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(
                (a.asname or a.name.split(".")[0], a.name if a.asname else a.name.split(".")[0])
                for a in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            aliases.update((a.asname or a.name, f"{node.module}.{a.name}") for a in node.names)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and (value := _boundary_name(node.value, aliases)):
                    aliases[target.id] = value
    return aliases


def _boundary_violations(text, own, modules=None, *, shipped=False):
    tree = ast.parse(text)
    aliases, modules = _boundary_bindings(tree), modules or {}
    found = set()
    for node in ast.walk(tree):
        if shipped:
            if _test_switch(node, aliases):
                found.add((node.lineno, "shipped test switch"))
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
            if any(_test_module(name) for name in names):
                found.add((node.lineno, "test module import"))
        name = _resolve_boundary(_boundary_name(node, aliases), modules)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and _within(name, f"agent_pm.{own}")
        ):
            found.add((node.lineno, "own project attribute mutation"))
        if isinstance(node, ast.Call):
            found.update(
                (node.lineno, reason) for reason in _boundary_calls(node, aliases, own, modules)
            )
    return sorted(found)


def _test_module(name):
    return any(part.endswith("_test") for part in name.split("."))


def _test_switch(node, aliases):
    candidates = []
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        candidates.append(node.id)
    if (
        isinstance(node, ast.Call)
        and _boundary_name(node.func, aliases) in {"os.getenv", "os.environ.get"}
        and node.args
    ):
        candidates.append(_boundary_name(node.args[0], aliases))
    if isinstance(node, ast.Subscript) and _boundary_name(node.value, aliases) == "os.environ":
        candidates.append(_boundary_name(node.slice, aliases))
    return any(name.endswith("_TEST") or name.startswith("FORCE_") for name in candidates)


def _boundary_calls(node, aliases, own, modules):
    arguments = node.args or [k.value for k in node.keywords if k.arg in {"target", "in_dict"}]
    if not arguments:
        return []
    call, raw = _boundary_name(node.func, aliases), _boundary_name(arguments[0], aliases)
    target = _resolve_boundary(raw, modules)
    rules = {
        "shipped code patch": call.endswith(".patch") and raw.startswith("agent_pm."),
        "own project object patch": call.endswith(".patch.object")
        and _within(target, f"agent_pm.{own}"),
        "sys.modules dictionary patch": call.endswith(".patch.dict") and target == "sys.modules",
        "own project attribute mutation": call in {"setattr", "delattr"}
        and _within(target, f"agent_pm.{own}"),
        "test module import": (call.endswith("import_module") or call == "__import__")
        and _test_module(raw),
    }
    return [reason for reason, violated in rules.items() if violated]


def _resolve_boundary(name, modules):
    seen = set()
    while name not in seen:
        seen.add(name)
        parts = name.split(".")
        matched = False
        for index in range(len(parts) - 1, 0, -1):
            module, member = ".".join(parts[:index]), parts[index]
            if module in modules and member in modules[module]:
                name = ".".join([modules[module][member], *parts[index + 1 :]])
                matched = True
                break
        if not matched:
            return name
    return name


def _source_modules(repo, projects):
    return {
        rel.removesuffix(".py").replace("/", ".").removesuffix(".__init__"): _boundary_bindings(
            ast.parse(text)
        )
        for project in projects
        for rel, text in _files(os.path.join(repo, project, "src"), ".py")
    }
