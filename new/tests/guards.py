"""Guards for the new core's rules. Each takes a root and returns its sorted violations, `<path relative to root>: <what>`
([] when clean); string constants include docstrings.

layer_imports(core)  the layers under src/ (LAYERS) import each other only as delegate → composer or driver, driver →
                     adapters, a module outside the layers → driver; outside driver/, of the driver only driver.api.
                     No module imports repo as a library. Imports read by ast: nested ones, relative ones resolved, literal-string import_module/__import__
runtime_names(core)  no runtime name (RUNTIME) in a .py or .toml outside src/adapters/; in a .toml, comments, a table
                     header [adapters.<name>…], an implementation = "adapters.<name>:<Class>Adapter" line and a
                     runtime = "<name>" line under any table (the config naming an Adapter) are exempt
old_core(new)        no import of top-level `core` and no string constant naming the old core's source dir (OLD)
adapters_key(core)   no string constant "adapters" outside src/driver/
syntax_311(new)      every .py parses as Python 3.11
pure_utils(core)     src/utils/ imports only the Python standard library
modes_import_api(core)  no module under src/driver/modes/ imports driver.api (a Mode raises driver.errors' DriverError)
tmux_calls(core)     no str constant "tmux" outside src/driver/tmux_host.py (TMUX_HOST), the only module that calls tmux
syntax_39(core)      each standalone file (STANDALONE) parses as Python 3.9
"""
import ast
import os
import re
import sys

LAYERS = ("adapters", "driver", "composer", "delegate")
OUTSIDE = "outside the layers"
ALLOWED = {("delegate", "composer"), ("delegate", "driver"), ("driver", "adapters"), (OUTSIDE, "driver")}
RUNTIME = re.compile(r"claude|codex", re.I)
TOML_CODE = re.compile(r"""(?:[^#"']|"(?:\\.|[^"\\])*"|'[^']*')*""")
TOML_HEADER = re.compile(r"\s*\[\s*adapters\.[a-z][a-z0-9_]*(\.[^\]]*)?\]\s*\Z")
TOML_IMPLEMENTATION = re.compile(
    r'\s*implementation\s*=\s*"adapters\.[a-z][a-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*Adapter"\s*\Z')
TOML_RUNTIME = re.compile(r'\s*runtime\s*=\s*"[a-z][a-z0-9_]*"\s*\Z')
OLD = "/".join(("core", "src"))   # built at run time, so this file never names it
OLD_NAMED = re.compile("(?<!new/)" + OLD)
TMUX_HOST = "src/driver/tmux_host.py"
TUI_HOOK = "src/driver/modes/tui_hook.py"
STANDALONE = (TMUX_HOST, TUI_HOOK)   # run with -I as files of their own, under the system's Python 3.9 too


def layer_imports(core: str) -> list[str]:
    found = set()
    for rel, text in _files(core, ".py"):
        parts = rel[:-3].split("/")
        names = parts[1:] if parts[0] == "src" else None
        layer = names[0] if names and len(names) > 1 and names[0] in LAYERS else OUTSIDE
        for module in _imports(ast.parse(text, rel), names[:-1] if names else None):
            top = module.split(".")[0]
            if top == "repo" or (top in LAYERS and top != layer and ((layer, top) not in ALLOWED
                                                   or top == "driver" and module != "driver.api")):
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
                line = code if line[len(code):len(code) + 1] in ("", "#") else line   # an unclosed quote: whole line
                if TOML_HEADER.match(line) or TOML_IMPLEMENTATION.match(line) or TOML_RUNTIME.match(line):
                    continue
            found.update(f"{rel}:{number}: runtime name {word}" for word in RUNTIME.findall(line))
    return sorted(found)


def old_core(new: str) -> list[str]:
    found = set()
    for rel, text in _files(new, ".py"):
        tree = ast.parse(text, rel)
        found.update(f"{rel}: imports {m}" for m in _imports(tree, None) if m.split(".")[0] == "core")
        found.update(f"{rel}:{line}: names the old {OLD}" for line, s in _strings(tree) if OLD_NAMED.search(s))
    return sorted(found)


def adapters_key(core: str) -> list[str]:
    found = set()
    for rel, text in _files(core, ".py", skip="src/driver/"):
        found.update(f'{rel}:{line}: literal key "adapters"' for line, s in _strings(ast.parse(text, rel))
                     if s == "adapters")
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
            found.update(f"{rel}: imports {m}" for m in _imports(tree, names[:-1])
                         if m.split(".")[:2] == ["driver", "api"])
    return sorted(found)


def tmux_calls(core: str) -> list[str]:
    found = set()
    for rel, text in _files(core, ".py"):
        if rel != TMUX_HOST:
            found.update(f'{rel}:{line}: str constant "tmux"' for line, s in _strings(ast.parse(text, rel))
                         if s == "tmux")
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


def _src_modules(core):
    """(path relative to core, its module names under src/, its ast) of each .py in core's src/."""
    for rel, text in _files(core, ".py"):
        if rel.startswith("src/"):
            yield rel, rel[len("src/"):-3].split("/"), ast.parse(text, rel)


def _files(root, suffixes, skip=None):
    """(path relative to root with / separators, text) of each file under root ending in suffixes, but under skip."""
    for top, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(names):
            rel = os.path.relpath(os.path.join(top, name), root).replace(os.sep, "/")
            if name.endswith(suffixes) and not (skip and rel.startswith(skip)):
                with open(os.path.join(top, name), encoding="utf-8") as f:
                    yield rel, f.read()


def _imports(tree, package):
    """Each module tree imports. `package`, the importing module's package as a list of names, resolves relative
    imports (None: they are skipped). `from P import n` yields P.n when P is top-level (the driver's __init__ holds no
    names), else P."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if package is None or node.level - 1 > len(package):
                    continue
                parts = package[:len(package) - node.level + 1] + (node.module.split(".") if node.module else [])
            else:
                parts = node.module.split(".")
            if len(parts) > 1:
                yield ".".join(parts)
            else:
                yield from (".".join(parts + ([] if a.name == "*" else [a.name])) for a in node.names)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str) and (
                    isinstance(node.func, ast.Name) and node.func.id in ("__import__", "import_module")
                    or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"):
            yield node.args[0].value


def _strings(tree):
    """(line, value) of each str constant in tree, docstrings included."""
    return ((n.lineno, n.value) for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str))
