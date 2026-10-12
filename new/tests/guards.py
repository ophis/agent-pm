"""The new core's rules: each guard takes a root and returns its sorted violations; string constants include docstrings.

layer_imports(core)   imports go one way (new/README.md), none imports repo, nested, relative or import_module'd
runtime_names(core)   no runtime name (RUNTIME) outside src/adapters/ but a .toml's comments and Adapter lines
old_core(new)         no `core` import, no string constant naming OLD
adapters_key(core)    no string constant "adapters" outside src/driver/
syntax_311(new)       every .py parses as Python 3.11
pure_utils(core)      src/utils/ imports only the standard library
private_imports(root) no other module's _name
modes_import_api(core) src/driver/modes/ never imports driver.api
tmux_calls(core)      "tmux" only in TMUX_HOST
syntax_39(core)       STANDALONE files parse as Python 3.9
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
                if module.split(".")[0] not in sys.stdlib_module_names:
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
