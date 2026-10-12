"""The new core's rules: each guard takes a root and returns its sorted violations; string constants include docstrings.

layer_imports(core)   imports go one way (new/README.md), none imports agent_tools or a BARE name, nested, relative
                      or import_module'd
runtime_names(core)   no runtime name (RUNTIME) outside src/agent_pm/adapters/ but a .toml's comments and Adapter lines
old_core(new)         no `core` import, no string constant naming OLD
adapters_key(core)    no string constant "adapters" outside src/agent_pm/driver/
syntax_311(new)       every .py parses as Python 3.11
pure_utils(core)      utils and agent_tools import only the standard library and utils
private_imports(root) no other module's _name
modes_import_api(core) driver.modes never imports driver.api
tmux_calls(core)      "tmux" only in TMUX_HOST
syntax_39(core)       STANDALONE files parse as Python 3.9
import_hygiene(new)   no sys.path mutation, module-level environment write or E402/I001/isort comment; the import
                      root holds only agent_pm
"""

import ast
import io
import os
import re
import sys
import tokenize

LAYERS = ("adapters", "driver", "composer", "delegate")
OUTSIDE = "outside the layers"
ALLOWED = {
    ("delegate", "composer"),
    ("delegate", "driver"),
    ("driver", "adapters"),
    (OUTSIDE, "driver"),
}
TOOLS = "agent_tools"
PURE = ("utils", TOOLS)
BARE = (*LAYERS, *PURE, "repo")
RUNTIME = re.compile(r"claude|codex", re.I)
TOML_CODE = re.compile(r"""(?:[^#"']|"(?:\\.|[^"\\])*"|'[^']*')*""")
TOML_HEADER = re.compile(r"\s*\[\s*adapters\.[a-z][a-z0-9_]*(\.[^\]]*)?\]\s*\Z")
TOML_IMPLEMENTATION = re.compile(
    r'\s*implementation\s*=\s*"adapters\.[a-z][a-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*Adapter"\s*\Z'
)
TOML_RUNTIME = re.compile(r'\s*runtime\s*=\s*"[a-z][a-z0-9_]*"\s*\Z')
OLD = "/".join(("core", "src"))  # built at run time, so this file never names it
OLD_NAMED = re.compile("(?<!new/)" + OLD)
TMUX_HOST = "src/agent_pm/driver/tmux_host.py"
TUI_HOOK = "src/agent_pm/driver/modes/tui_hook.py"
STANDALONE = (TMUX_HOST, TUI_HOOK)
HYGIENE = re.compile(r"noqa\s*:[\s,\w]*\b(?:E402|I001)\b|isort\s*:", re.I)
READS = {"copy", "count", "get", "index", "items", "keys", "values"}


def layer_imports(core: str) -> list[str]:
    found = set()
    for rel, names, imports in _modules(core):
        layer = names[0] if names and len(names) > 1 and names[0] in LAYERS else OUTSIDE
        for module, inner in imports:
            first = inner.split(".")[0]
            if module.split(".")[0] in BARE:
                found.add(f"{rel}: bare import {module}")
            elif first == TOOLS or (
                first in LAYERS
                and first != layer
                and ((layer, first) not in ALLOWED or first == "driver" and inner != "driver.api")
            ):
                found.add(f"{rel}: {layer} imports {module}")
    return sorted(found)


def pure_utils(core: str) -> list[str]:
    found = set()
    for rel, names, imports in _modules(core):
        if names and names[0] in PURE:
            found.update(
                f"{rel}: {names[0]} imports {m}"
                for m, inner in imports
                if inner.split(".")[0] != "utils" and m.split(".")[0] not in sys.stdlib_module_names
            )
    return sorted(found)


def runtime_names(core: str) -> list[str]:
    found = set()
    for rel, text in _files(core, (".py", ".toml"), skip="src/agent_pm/adapters/"):
        for number, line in enumerate(text.splitlines(), 1):
            if rel.endswith(".toml"):
                code = TOML_CODE.match(line)[0]
                line = code if line[len(code) : len(code) + 1] in ("", "#") else line
                if (
                    TOML_HEADER.match(line)
                    or TOML_IMPLEMENTATION.match(line)
                    or TOML_RUNTIME.match(line)
                ):
                    continue
            found.update(f"{rel}:{number}: runtime name {word}" for word in RUNTIME.findall(line))
    return sorted(found)


def old_core(new: str) -> list[str]:
    found = set()
    for rel, text in _files(new, ".py"):
        tree = ast.parse(text, rel)
        found.update(
            f"{rel}: imports {m}" for m in _imports(tree, None) if m.split(".")[0] == "core"
        )
        found.update(
            f"{rel}:{line}: names the old {OLD}"
            for line, s in _strings(tree)
            if OLD_NAMED.search(s)
        )
    return sorted(found)


def adapters_key(core: str) -> list[str]:
    found = set()
    for rel, text in _files(core, ".py", skip="src/agent_pm/driver/"):
        found.update(
            f'{rel}:{line}: literal key "adapters"'
            for line, s in _strings(ast.parse(text, rel))
            if s == "adapters"
        )
    return sorted(found)


def syntax_311(new: str) -> list[str]:
    found = []
    for rel, text in _files(new, ".py"):
        try:
            ast.parse(text, rel, feature_version=(3, 11))
        except SyntaxError as e:
            found.append(f"{rel}: {e}")
    return sorted(found)


def modes_import_api(core: str) -> list[str]:
    found = set()
    for rel, names, imports in _modules(core):
        if names and names[:2] == ["driver", "modes"]:
            found.update(
                f"{rel}: imports {m}"
                for m, inner in imports
                if inner.split(".")[:2] == ["driver", "api"]
            )
    return sorted(found)


def tmux_calls(core: str) -> list[str]:
    found = set()
    for rel, text in _files(core, ".py"):
        if rel != TMUX_HOST:
            found.update(
                f'{rel}:{line}: str constant "tmux"'
                for line, s in _strings(ast.parse(text, rel))
                if s == "tmux"
            )
    return sorted(found)


def syntax_39(core: str) -> list[str]:
    found = []
    for rel, text in _files(core, ".py"):
        if rel in STANDALONE:
            try:
                ast.parse(text, rel, feature_version=(3, 9))
            except SyntaxError as e:
                found.append(f"{rel}: {e}")
    return sorted(found)


def private_imports(root: str) -> list[str]:
    found = set()
    for rel, text in _files(root, ".py"):
        tree = ast.parse(text, rel)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(a.asname or a.name for a in node.names)
                module = "." * node.level + (f"{node.module}." if node.module else "")
                found.update(
                    f"{rel}:{node.lineno}: private {module}{a.name}"
                    for a in node.names
                    if _private(a.name)
                )
            elif isinstance(node, ast.Import):
                imported.update(a.asname or a.name.split(".")[0] for a in node.names)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and _private(node.attr):
                chain, base = [node.attr], node.value
                while isinstance(base, ast.Attribute):
                    chain.append(base.attr)
                    base = base.value
                if isinstance(base, ast.Name) and base.id in imported:
                    found.add(
                        f"{rel}:{node.lineno}: private {'.'.join([base.id, *reversed(chain)])}"
                    )
    return sorted(found)


def import_hygiene(new: str) -> list[str]:
    found = set()
    for rel, text in _files(new, ".py"):
        tree = ast.parse(text, rel)
        nodes = list(ast.walk(tree))
        path, environ, putenv = (
            _spellings(nodes, n) for n in ("sys.path", "os.environ", "os.putenv")
        )
        found.update(f"{rel}:{n.lineno}: sys.path mutation" for n in nodes if _changes(n, path))
        found.update(
            f"{rel}:{n.lineno}: module-level environment write"
            for n in _at_import(tree)
            if _changes(n, environ) or isinstance(n, ast.Call) and _is(n.func, putenv)
        )
        found.update(
            f"{rel}:{t.start[0]}: comment {t.string}"
            for t in tokenize.generate_tokens(io.StringIO(text).readline)
            if t.type == tokenize.COMMENT and HYGIENE.search(t.string)
        )
    src = os.path.join("core", "src")
    root = os.path.join(new, src)
    found.update(
        f"{src}/{entry}: not agent_pm"
        for entry in (os.listdir(root) if os.path.isdir(root) else ())
        if entry not in ("agent_pm", "__pycache__")
    )
    return sorted(found)


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


def _private(name):
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _modules(core):
    src = os.path.join(core, "src")
    dirs = sorted(os.listdir(src)) if os.path.isdir(src) else []
    dirs = [d for d in dirs if d != "__pycache__" and os.path.isdir(os.path.join(src, d))]
    top = dirs[0] if len(dirs) == 1 else None
    for rel, text in _files(core, ".py"):
        parts = rel[:-3].split("/")
        names = parts[2:] if top and parts[:2] == ["src", top] else None
        imports = _imports(ast.parse(text, rel), [top, *names[:-1]] if names else None, top)
        yield rel, names, [(m, _inner(m, top)) for m in imports]


def _inner(module, top):
    return module[len(top) + 1 :] if top and module.startswith(f"{top}.") else ""


def _files(root, suffixes, skip=None):
    for top, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            rel = os.path.relpath(os.path.join(top, name), root).replace(os.sep, "/")
            if name.endswith(suffixes) and not (skip and rel.startswith(skip)):
                with open(os.path.join(top, name), encoding="utf-8") as f:
                    yield rel, f.read()


def _imports(tree, package, top=None):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if package is None or node.level - 1 > len(package):
                    continue
                parts = package[: len(package) - node.level + 1] + (
                    node.module.split(".") if node.module else []
                )
            else:
                parts = node.module.split(".")
            if len(parts) > 1 + (parts[0] == top):
                yield ".".join(parts)
            else:
                yield from (
                    ".".join(parts + ([] if a.name == "*" else [a.name])) for a in node.names
                )
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id in ("__import__", "import_module")
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            )
        ):
            yield node.args[0].value


def _strings(tree):
    return (
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
