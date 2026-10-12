"""The new tree's rules: each guard takes a root and returns its sorted violations; string constants include docstrings.

layer_imports(core)   imports go one way (new/README.md), none imports repo, nested, relative or import_module'd
runtime_names(core)   no runtime name (RUNTIME) outside src/adapters/ but a .toml's comments and Adapter lines
old_core(new)         no `core` import, no string constant naming OLD
adapters_key(core)    no string constant "adapters" outside src/driver/
syntax_311(new)       every .py parses as Python 3.11
pure_utils(core)      src/utils/ imports only the standard library and utils
private_imports(root) no other module's _name
modes_import_api(core) src/driver/modes/ never imports driver.api
tmux_calls(core)      "tmux" only in TMUX_HOST
syntax_39(core)       STANDALONE files parse as Python 3.9
orchestrator_imports(orch) src/ imports stdlib, siblings, utils, `from driver import api`, delegate's __all__ (and
                      composer's, in config.py only); no module named as in NAMED
orchestrator_text(orch) no runtime name but CLAUDE.md, no string "tmux" or "adapters"
config_once(orch)     config.load, linear.use_logs only in main of ENTRIES; LOAD_HELPERS only in config.py
"""

import ast
import os
import re
import sys

LAYERS = ("adapters", "driver", "composer", "delegate")
OUTSIDE = "outside the layers"
ALLOWED = {
    ("delegate", "composer"),
    ("delegate", "driver"),
    ("driver", "adapters"),
    (OUTSIDE, "driver"),
}
RUNTIME = re.compile(r"claude|codex", re.I)
TOML_CODE = re.compile(r"""(?:[^#"']|"(?:\\.|[^"\\])*"|'[^']*')*""")
TOML_HEADER = re.compile(r"\s*\[\s*adapters\.[a-z][a-z0-9_]*(\.[^\]]*)?\]\s*\Z")
TOML_IMPLEMENTATION = re.compile(
    r'\s*implementation\s*=\s*"adapters\.[a-z][a-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*Adapter"\s*\Z'
)
TOML_RUNTIME = re.compile(r'\s*runtime\s*=\s*"[a-z][a-z0-9_]*"\s*\Z')
OLD = "/".join(("core", "src"))  # built at run time, so this file never names it
OLD_NAMED = re.compile("(?<!new/)" + OLD)
TMUX_HOST = "src/driver/tmux_host.py"
TUI_HOOK = "src/driver/modes/tui_hook.py"
STANDALONE = (TMUX_HOST, TUI_HOOK)
CORE_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "src")
EXPORTERS = ("delegate", "composer")
DENIED = {"adapters", "repo", "roster", "core", "tmux_host", "attended"}
NAMED = {*LAYERS, "utils", "repo", "attended"}
TOOL_STRINGS = ("tmux", "adapters")
TOML_STRING = re.compile(r""""((?:\\.|[^"\\])*)"|'([^']*)'""")
ENTRIES = ("src/run.py", "src/promote.py", "src/router.py")
ONCE = ("config.load", "linear.use_logs")
LOAD_HELPERS = ("runnable", "run_config", "docs")


def layer_imports(core: str) -> list[str]:
    found = set()
    for rel, text in _files(core, ".py"):
        parts = rel[:-3].split("/")
        names = parts[1:] if parts[0] == "src" else None
        layer = names[0] if names and len(names) > 1 and names[0] in LAYERS else OUTSIDE
        for module in _imports(ast.parse(text, rel), names[:-1] if names else None):
            top = module.split(".")[0]
            if top == "repo" or (
                top in LAYERS
                and top != layer
                and ((layer, top) not in ALLOWED or top == "driver" and module != "driver.api")
            ):
                found.add(f"{rel}: {layer} imports {module}")
    return sorted(found)


def pure_utils(core: str) -> list[str]:
    found = set()
    for rel, text in _files(core, ".py"):
        if rel.startswith("src/utils/"):
            package = rel[:-3].split("/")[1:-1]
            for module in _imports(ast.parse(text, rel), package):
                if module.split(".")[0] not in sys.stdlib_module_names | {"utils"}:
                    found.add(f"{rel}: utils imports {module}")
    return sorted(found)


def runtime_names(core: str) -> list[str]:
    found = set()
    for rel, text in _files(core, (".py", ".toml"), skip="src/adapters/"):
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
    for rel, text in _files(core, ".py", skip="src/driver/"):
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
    for rel, names, tree in _src_modules(core):
        if names[:2] == ["driver", "modes"]:
            found.update(
                f"{rel}: imports {m}"
                for m in _imports(tree, names[:-1])
                if m.split(".")[:2] == ["driver", "api"]
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


def orchestrator_imports(orch: str) -> list[str]:
    found = set()
    src = {rel: text for rel, text in _files(orch, ".py") if rel.startswith("src/")}
    siblings = {rel[4:-3] for rel in src if "/" not in rel[4:]}
    found.update(f"{rel}: module named {rel[4:-3]}" for rel in src if rel[4:-3] in NAMED)
    exported = {package: _exported(package) for package in EXPORTERS}
    for rel, text in src.items():
        tree = ast.parse(text, rel)
        for line, module, name in _import_items(tree):
            if _denied(rel, module, name, siblings, exported):
                full = (
                    module
                    if name is None
                    else module + name
                    if module.endswith(".")
                    else f"{module}.{name}"
                )
                found.add(f"{rel}:{line}: imports {full}")
        bound = _bindings(tree)
        found.update(
            f"{rel}:{n.lineno}: uses {bound[n.value.id]}.{n.attr}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and bound.get(n.value.id) in exported
            and n.attr not in exported[bound[n.value.id]]
        )
    return sorted(found)


def orchestrator_text(orch: str) -> list[str]:
    found = set()
    for rel, text in _files(orch, (".py", ".toml")):
        lines = text.splitlines()
        for number, line in enumerate(lines, 1):
            found.update(
                f"{rel}:{number}: runtime name {w}"
                for w in RUNTIME.findall(line.replace("CLAUDE.md", " "))
            )
        strings = (
            _strings(ast.parse(text, rel))
            if rel.endswith(".py")
            else (
                (number, m[1] or m[2])
                for number, line in enumerate(lines, 1)
                for m in TOML_STRING.finditer(TOML_CODE.match(line)[0])
            )
        )
        found.update(
            f'{rel}:{line}: str constant "{s}"' for line, s in strings if s in TOOL_STRINGS
        )
    return sorted(found)


def config_once(orch: str) -> list[str]:
    found = set()
    for rel, text in _files(orch, ".py"):
        if not rel.startswith("src/"):
            continue
        tree = ast.parse(text, rel)
        bound = _bindings(tree)
        mains = {
            id(n)
            for top in tree.body
            if rel in ENTRIES and isinstance(top, ast.FunctionDef) and top.name == "main"
            for n in ast.walk(top)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called = _dotted(node.func, bound)
                if called in ONCE and id(node) not in mains:
                    found.add(f"{rel}:{node.lineno}: calls {called} outside an entry main")
                elif called.rpartition(".")[2] in LOAD_HELPERS and rel != "src/config.py":
                    found.add(f"{rel}:{node.lineno}: calls {called}")
    return sorted(found)


def _denied(rel, module, name, siblings, exported):
    top = module.split(".")[0]
    if top in DENIED:
        return True
    if top in exported:
        return (
            "." in module
            or name is not None
            and name not in exported[top]
            or (top == "composer" and rel != "src/config.py")
        )
    if top == "driver":
        return (module, name) != ("driver", "api")
    return not (top in sys.stdlib_module_names or top in siblings or top == "utils")


def _exported(package):
    with open(os.path.join(CORE_SRC, package, "__init__.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return next(
        set(ast.literal_eval(n.value))
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets)
    )


def _import_items(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, a.name, None) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield from (
                (node.lineno, "." * node.level + (node.module or ""), a.name) for a in node.names
            )
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and _dotted(node.func, {}).rpartition(".")[2] in ("__import__", "import_module")
        ):
            yield node.lineno, node.args[0].value, None


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


def _src_modules(core):
    for rel, text in _files(core, ".py"):
        if rel.startswith("src/"):
            yield rel, rel[len("src/") : -3].split("/"), ast.parse(text, rel)


def _files(root, suffixes, skip=None):
    for top, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            rel = os.path.relpath(os.path.join(top, name), root).replace(os.sep, "/")
            if name.endswith(suffixes) and not (skip and rel.startswith(skip)):
                with open(os.path.join(top, name), encoding="utf-8") as f:
                    yield rel, f.read()


def _imports(tree, package):
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
            if len(parts) > 1:
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
