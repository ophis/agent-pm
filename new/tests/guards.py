"""The new tree's rules: each guard takes a root and returns its sorted violations; string constants include docstrings.

layer_imports(core)   imports go one way (new/README.md), a layer imports no package module outside LAYERS, SESSION
                      and SHARED, a session layer no role module but UPWARD, none imports TOOLS, ORCH or a BARE name,
                      nested, relative or import_module'd
role_session(core)    role/ imports SESSION only by an __all__ name
layer_commands(new)   COMMAND_SRC's SPAWNS calls pass no str starting COMMAND (positional or args=), SESSION_SRC's
                      but SESSION_COMMAND
core_standalone(core) no file names LINEAR
runtime_names(core)   no runtime name (RUNTIME) outside session/adapters/ but a .toml's comments and Adapter lines
old_core(new)         no `core` import, no str constant naming OLD
adapters_key(core)    no str constant "adapters" outside session/driver/
syntax_311(new)       every .py parses as Python 3.11
pure_utils(core)      each PURE unit imports only stdlib and its PURE entry
private_imports(root) no other module's _name
modes_import_api(core) driver.modes never imports driver.api
tmux_calls(core)      "tmux" only in TMUX_HOST
syntax_39(core)       STANDALONE files parse as Python 3.9
import_hygiene(new)   no sys.path mutation, module-level environment write or E402/I001/isort comment; ROOTS'
                      src/ hold only their package
python_commands(new)  PYTHON in COMMAND_FILES and COMMAND_DOCS' .md is PYTHON_OK
orchestrator_imports(orch) src/ imports stdlib, ORCH, SHARED, `from session.driver import api`, EXPORTERS' __all__
                      (composer's in config.py only), no bare name; any module name (packages cannot clash)
orchestrator_text(orch) no runtime name but CLAUDE.md, no string "tmux" or "adapters"; HANDOFF only in HANDOFF_HOME
config_once(orch)     ONCE called only in main of ENTRIES; LOAD_HELPERS only in config.py
"""

import ast
import io
import os
import re
import sys
import tokenize

GROUPS = ("session", "role", "manager")
DRIVER = "session.driver"
DRIVER_API = f"{DRIVER}.api"
MODES = f"{DRIVER}.modes"
LAYERS = ("session.adapters", DRIVER, "role.composer", "role.delegate")
SESSION = "session"
OUTSIDE = "outside the layers"
ALLOWED = {
    ("role.delegate", "role.composer"),
    ("role.delegate", SESSION),
    (DRIVER, "session.adapters"),
    (OUTSIDE, SESSION),
    (OUTSIDE, DRIVER),
}
SHARED = ("utils", "config", "role.contract", "role.repos")
# until TASK-366 (356-5) deletes print_prompt
UPWARD = {("session.adapters.print_prompt.in_process", "role.contract")}
TOOLS = "role.agent_tools"
PURE = {
    "utils": ("utils",),
    "config": ("utils", "config"),
    "role.contract": SHARED,
    "role.repos": SHARED,
    TOOLS: ("utils", "config", "role.repos"),
}
BARE = (*GROUPS, *(layer.rpartition(".")[2] for layer in LAYERS), "utils", "agent_tools", "repo")
RUNTIME = re.compile(r"claude|codex|print_prompt", re.I)
TOML_CODE = re.compile(r"""(?:[^#"']|"(?:\\.|[^"\\])*"|'[^']*')*""")
TOML_HEADER = re.compile(r"\s*\[\s*(?:adapters|policy)\.[a-z][a-z0-9_]*(\.[^\]]*)?\]\s*\Z")
TOML_IMPLEMENTATION = re.compile(
    r'\s*implementation\s*=\s*"adapters\.[a-z][a-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*Adapter"\s*\Z'
)
TOML_RUNTIME = re.compile(r'\s*runtime\s*=\s*"[a-z][a-z0-9_]*"\s*\Z')
OLD = "/".join(("core", "src"))  # built at run time, so this file never names it
OLD_NAMED = re.compile("(?<!new/)" + OLD)
TMUX_HOST = "src/agent_pm/session/driver/tmux_host.py"
TUI_HOOK = "src/agent_pm/session/driver/modes/tui_hook.py"
STANDALONE = (TMUX_HOST, TUI_HOOK)
HYGIENE = re.compile(r"noqa\s*:[\s,\w]*\b(?:E402|I001)\b|isort\s*:", re.I)
READS = {"copy", "count", "get", "index", "items", "keys", "values"}
CORE_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "src")
CORE_PKG = "agent_pm"
ORCH = "agent_pm_linear"
ORCH_SRC = f"src/{ORCH}/"
ROOTS = (("core", CORE_PKG), ("linear-orchestrator", ORCH))
CORE_TOP = f"{ROOTS[0][0]}/src/{CORE_PKG}/"
SESSION_SRC = f"{CORE_TOP}{SESSION}/"
COMMAND_SRC = (SESSION_SRC, f"{CORE_TOP}role/", f"{ROOTS[1][0]}/{ORCH_SRC}")
COMMAND = "agent-pm-"
SESSION_COMMAND = "agent-pm-session"
SPAWNS = re.compile(r"subprocess\.\w+|os\.(?:exec|spawn)\w*")
LINEAR = re.compile(f"{ROOTS[1][0]}|{ORCH}")
COMPOSER = f"{CORE_PKG}.role.composer"
EXPORTERS = (f"{CORE_PKG}.role.delegate", COMPOSER)
CONFIG_PY = f"{ORCH_SRC}config.py"
TOOL_STRINGS = ("tmux", "adapters")
TOML_STRING = re.compile(r""""((?:\\.|[^"\\])*)"|'([^']*)'""")
ENTRIES = tuple(f"{ORCH_SRC}{name}.py" for name in ("run", "promote", "router"))
ONCE = (f"{ORCH}.config.load", f"{ORCH}.linear.use_logs")
LOAD_HELPERS = ("runnable", "run_config", "docs")
HANDOFF = re.compile(r"Handoff from|## (?:Source|Instructions|Comments)")
HANDOFF_HOME = f"{ORCH_SRC}issues.py"
COMMAND_FILES = ("core/config.toml", "linear-orchestrator/config.toml")
COMMAND_DOCS = ("core/skills/", "core/team/")
PYTHON = re.compile(r"\bpython(?:3(?:\.\d+)?)?\b(?:[ \t]+[^\s`'\"]+){0,3}")
PYTHON_OK = re.compile(rf"\S+[ \t]+-P[ \t]+-m[ \t]+(?:{CORE_PKG}|{ORCH})\.\w")


def layer_imports(core: str) -> list[str]:
    found = set()
    for rel, names, imports in _modules(core):
        name = ".".join(names or ())
        layer = next((each for each in LAYERS if name.startswith(f"{each}.")), OUTSIDE)
        for module, inner in imports:
            unit = _unit(inner) if inner else module.split(".")[0]
            if module.split(".")[0] in BARE:
                found.add(f"{rel}: bare import {module}")
            elif (
                unit in (TOOLS, ORCH)
                or (layer != OUTSIDE and inner and unit not in (*LAYERS, SESSION, *SHARED))
                or (
                    _within(layer, "session")
                    and _within(unit, "role")
                    and (name, unit) not in UPWARD
                )
                or (
                    unit in (*LAYERS, SESSION)
                    and unit != layer
                    and ((layer, unit) not in ALLOWED or unit == DRIVER and inner != DRIVER_API)
                )
            ):
                found.add(f"{rel}: {layer} imports {module}")
    return sorted(found)


def role_session(core: str) -> list[str]:
    found = set()
    exported = _all(os.path.join(core, "src", _top(core) or "", SESSION, "__init__.py"))
    for rel, names, imports in _modules(core):
        if names and names[0] == "role":
            found.update(
                f"{rel}: imports {m} outside session.__all__"
                for m, inner in imports
                if _within(inner, SESSION) and inner.partition(".")[2] not in exported
            )
    return sorted(found)


def layer_commands(new: str) -> list[str]:
    found = set()
    for scanned in COMMAND_SRC:
        for rel, text in _files(os.path.join(new, scanned), ".py"):
            tree = ast.parse(text, rel)
            bound = _bindings(tree)
            for call in ast.walk(tree):
                if isinstance(call, ast.Call) and SPAWNS.fullmatch(_dotted(call.func, bound)):
                    argv = [*call.args, *(k.value for k in call.keywords if k.arg == "args")]
                    words = {
                        (line, s.split()[0])
                        for arg in argv
                        for line, s in _strings(arg)
                        if s.startswith(COMMAND)
                    }
                    found.update(
                        f'{scanned}{rel}:{line}: runs "{word}"'
                        for line, word in words
                        if not (scanned == SESSION_SRC and word == SESSION_COMMAND)
                    )
    return sorted(found)


def core_standalone(core: str) -> list[str]:
    found = set()
    for top, dirs, names in os.walk(core):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(names):
            rel = os.path.relpath(os.path.join(top, name), core).replace(os.sep, "/")
            with open(os.path.join(top, name), encoding="utf-8", errors="replace") as f:
                for number, line in enumerate(f, 1):
                    found.update(f"{rel}:{number}: names {w}" for w in LINEAR.findall(line))
    return sorted(found)


def pure_utils(core: str) -> list[str]:
    found = set()
    for rel, names, imports in _modules(core):
        package = next((p for p in PURE if _within(".".join(names or ()), p)), None)
        if package:
            found.update(
                f"{rel}: {package} imports {m}"
                for m, inner in imports
                if not any(_within(inner, u) for u in PURE[package])
                and m.split(".")[0] not in sys.stdlib_module_names
            )
    return sorted(found)


def runtime_names(core: str) -> list[str]:
    found = set()
    for rel, text in _files(core, (".py", ".toml"), skip="src/agent_pm/session/adapters/"):
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
    for rel, text in _files(core, ".py", skip="src/agent_pm/session/driver/"):
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
        if names and _within(".".join(names), MODES):
            found.update(
                f"{rel}: imports {m}" for m, inner in imports if _within(inner, DRIVER_API)
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
    for top, package in ROOTS:
        root = os.path.join(new, top, "src")
        found.update(
            f"{top}/src/{entry}: not {package}"
            for entry in (os.listdir(root) if os.path.isdir(root) else ())
            if entry not in (package, "__pycache__")
        )
    return sorted(found)


def python_commands(new: str) -> list[str]:
    found = set()
    for rel, text in _files(new, (".toml", ".md")):
        if rel in COMMAND_FILES or rel.endswith(".md") and rel.startswith(COMMAND_DOCS):
            found.update(
                f'{rel}:{number}: python command "{m[0]}"'
                for number, line in enumerate(text.splitlines(), 1)
                for m in PYTHON.finditer(line)
                if not PYTHON_OK.match(m[0])
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


def orchestrator_imports(orch: str) -> list[str]:
    found = set()
    exported = {package: _exported(package) for package in EXPORTERS}
    for rel, text in _files(orch, ".py"):
        if not rel.startswith("src/"):
            continue
        tree = ast.parse(text, rel)
        for line, module, name in _import_items(tree):
            if _denied(rel, module, name, exported):
                full = (
                    module
                    if name is None
                    else module + name
                    if module.endswith(".")
                    else f"{module}.{name}"
                )
                found.add(f"{rel}:{line}: imports {full}")
        bound = _bindings(tree)
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute):
                base = _dotted(n.value, bound)
                if base in exported and n.attr not in exported[base]:
                    found.add(f"{rel}:{n.lineno}: uses {base}.{n.attr}")
    return sorted(found)


def orchestrator_text(orch: str) -> list[str]:
    found = set()
    for rel, text in _files(orch, (".py", ".toml")):
        lines = text.splitlines()
        grammar = rel.startswith("src/") and rel.endswith(".py") and rel != HANDOFF_HOME
        for number, line in enumerate(lines, 1):
            found.update(
                f"{rel}:{number}: runtime name {w}"
                for w in RUNTIME.findall(line.replace("CLAUDE.md", " "))
            )
            if grammar:
                found.update(f"{rel}:{number}: Handoff literal {w}" for w in HANDOFF.findall(line))
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
                elif called.rpartition(".")[2] in LOAD_HELPERS and rel != CONFIG_PY:
                    found.add(f"{rel}:{node.lineno}: calls {called}")
    return sorted(found)


def _denied(rel, module, name, exported):
    top = module.split(".")[0]
    if top in sys.stdlib_module_names or top == ORCH:
        return False
    packages = (CORE_PKG, *(f"{CORE_PKG}.{g}" for g in GROUPS))
    target = f"{module}.{name}" if module in packages and name else module
    if target in exported:
        return (
            module == target
            and name is not None
            and name not in exported[target]
            or (target == COMPOSER and rel != CONFIG_PY)
        )
    return not (
        any(_within(target, f"{CORE_PKG}.{s}") for s in SHARED)
        or (module, name) == (f"{CORE_PKG}.{DRIVER}", "api")
    )


def _exported(package):
    return _all(os.path.join(CORE_SRC, *package.split("."), "__init__.py"))


def _all(path):
    if not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return next(
        (
            set(ast.literal_eval(n.value))
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets)
        ),
        set(),
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


def _top(core):
    src = os.path.join(core, "src")
    dirs = sorted(os.listdir(src)) if os.path.isdir(src) else []
    dirs = [d for d in dirs if d != "__pycache__" and os.path.isdir(os.path.join(src, d))]
    return dirs[0] if len(dirs) == 1 else None


def _modules(core):
    top = _top(core)
    for rel, text in _files(core, ".py"):
        parts = rel[:-3].split("/")
        names = parts[2:] if top and parts[:2] == ["src", top] else None
        imports = _imports(ast.parse(text, rel), [top, *names[:-1]] if names else None, top)
        yield rel, names, [(m, _inner(m, top)) for m in imports]


def _unit(inner):
    return next((u for u in (*LAYERS, *SHARED, TOOLS) if _within(inner, u)), inner.split(".")[0])


def _within(name, unit):
    return name == unit or name.startswith(f"{unit}.")


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
            grouped = parts[0] == top and len(parts) > 1 and parts[1] in GROUPS
            if len(parts) > 1 + (parts[0] == top) + grouped:
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
