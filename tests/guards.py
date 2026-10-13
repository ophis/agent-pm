"""The repo's rules: each guard takes a root, returns its sorted violations; str constants include docstrings.
Only a TREES walk enters a project's tests/.

layer_imports(repo)   imports go one way (CLAUDE.md › Import rules), a unit imports no package module outside UNITS, SESSION
                      and SHARED, a session unit no delegate module, none imports TOOLS, ORCH or a BARE
                      name, nested, relative or import_module'd; SESSION_INTERNAL only from session/
delegate_session(repo) delegate/ imports SESSION only by an __all__ name
layer_commands(repo)  COMMAND_SRC's SPAWNS calls pass no str starting COMMAND, SESSION_SRC's but SESSION_COMMAND
standalone(repo)      no file names LINEAR but CONFIG_LEVELS' LEVELS line (the config override order)
runtime_names(repo)   no RUNTIME name outside session/adapters/ but a .toml's comments and Adapter lines
old_core(repo)        no `core` import
session_only(repo)    session/ imports only stdlib, session and utils, names none of SESSION_WORDS
adapters_key(repo)    no str constant "adapters" outside ADAPTERS_HOME
syntax_311(repo)      every .py parses as Python 3.11
pure_utils(repo)      each PURE unit imports only stdlib and its PURE entry
manager_imports(repo) nothing else imports MANAGER; it: stdlib, MANAGER_ALLOWED, MANAGER_EXCEPTIONS
private_imports(root) no other module's _name
modes_import_api(repo) MODES never imports SESSION_API
tmux_calls(repo)      "tmux" only in TMUX_HOST
status_log_format(repo) STATUS_LOG_CLOCK only in STATUS_LOG_HOME
syntax_39(repo)       PY39 files parse as Python 3.9
import_hygiene(repo)  no sys.path mutation, module-level environment write or E402/I001/isort comment
namespace_package(repo) each PROJECTS' src/ holds only PKG, no __init__.py, exactly its own subpackage (LEFTOVERS aside)
package_top(repo)     a LAYERS package but utils' holds only TOP, SUBPACKAGES and TOP_EXCEPTIONS
shared_types(repo)    a dataclass another module of its project uses, imported or as module.Name (an __init__ aside),
                      is in a *.types module or TYPE_HOMES
python_commands(repo) PYTHON in COMMAND_FILES and COMMAND_DOCS' .md is PYTHON_OK
orchestrator_imports(orch) src/ imports stdlib, ORCH, SHARED but CONFIG, session's __all__ (until TASK-372), EXPORTERS' __all__
                      (composer's and `from session import Session` in config.py only), no bare name
orchestrator_text(orch) no runtime name but CLAUDE.md, no string "tmux" or "adapters"; HANDOFF only in HANDOFF_HOME
config_once(orch)     ONCE called only in main of ENTRIES; LOAD_HELPERS only in config.py
test_layers(repo)     a <project>/tests/ file imports no root `tests` and no project after its own in LAYERS
config_levels(repo)   CONFIG_LEVELS' LEVELS holds each project once
explicit_keys(repo)   each `# local:` example key in a <project>/config.toml has a real line in one: the same dotted key,
                      the same table, depth and last key, a suffix of it, or a key of an empty real table

"""

import ast
import io
import os
import re
import sys
import tokenize
import tomllib

GROUPS = ("session", "delegate", "manager")
SESSION_API = "session.engine.api"
SESSION_TYPES = "session.engine.types"
MODES = "session.modes"
RECORDS = "session.engine.records"
HOSTS = "session.hosts"
HOST_ABC = f"{HOSTS}.base"  # the Host ABC: outside the units, in unit SESSION
UNITS = (
    "session.adapters",
    SESSION_API,
    SESSION_TYPES,
    MODES,
    RECORDS,
    HOSTS,
    "delegate.composer",
    "delegate",
)
SESSION = "session"
OUTSIDE = "outside the units"
CONFIG = "delegate.engine.config"
ALLOWED = {
    ("delegate", "delegate.composer"),
    ("delegate", SESSION),
    ("delegate.event.outcome", "delegate.composer"),  # SCHEMA, RunConfig
    *((SESSION_API, unit) for unit in ("session.adapters", SESSION_TYPES, MODES, RECORDS, HOSTS)),
    *((MODES, unit) for unit in ("session.adapters", SESSION_TYPES, HOSTS)),
    ("session.adapters", SESSION_TYPES),
    ("session.adapters.registry", HOSTS),  # until TASK-368
    ("session.engine.base", "session.adapters"),  # Session.configure stores into the registry
    ("session.engine.base", HOSTS),  # Session.parse_hosts
    (OUTSIDE, SESSION),
}
SESSION_INTERNAL = (SESSION_API, SESSION_TYPES)
SHARED = ("utils", CONFIG, "delegate.event")
TOOLS = "delegate.team.tools"
ORCH = "linear_orchestrator"
ALL_UNITS = (*UNITS, *SHARED, TOOLS)
PURE = {
    "utils": ("utils",),
    CONFIG: ("utils", CONFIG),
    "delegate.event": SHARED,
    "delegate.event.outcome": (*SHARED, "delegate.composer"),
    TOOLS: ("utils", CONFIG),
}
BARE = (
    *GROUPS,
    *(n for n in (u.rpartition(".")[2] for u in UNITS) if n not in sys.stdlib_module_names),
    "utils",
    TOOLS.rpartition(".")[2],
    "repo",
    ORCH,
)
SESSION_WORDS = (
    "config_file",
    "tomllib",
    "agent_pm.delegate.engine.config",
    "agent-pm-progress",
    "agent-pm-outcome",
    "team/",
)
RUNTIME = re.compile(r"claude|codex|print_prompt", re.I)
TOML_CODE = re.compile(r"""(?:[^#"']|"(?:\\.|[^"\\])*"|'[^']*')*""")
TOML_HEADER = re.compile(r"\s*\[\s*(?:adapters|policy)\.[a-z][a-z0-9_]*(\.[^\]]*)?\]\s*\Z")
TOML_IMPLEMENTATION = re.compile(
    r'\s*implementation\s*=\s*"adapters\.[a-z][a-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*Adapter"\s*\Z'
)
TOML_RUNTIME = re.compile(r'\s*runtime\s*=\s*"[a-z][a-z0-9_]*"\s*\Z')
TMUX_HOST = "session/src/agent_pm/session/hosts/tmux/tmux_host.py"
TUI_HOOK = "session/src/agent_pm/session/modes/tui_hook.py"
PY39 = (TMUX_HOST, TUI_HOOK)
HYGIENE = re.compile(r"noqa\s*:[\s,\w]*\b(?:E402|I001)\b|isort\s*:", re.I)
READS = {"copy", "count", "get", "index", "items", "keys", "values"}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "agent_pm"
ORCH_PKG = f"{PKG}.{ORCH}"
ORCH_SRC = f"src/{PKG}/{ORCH}/"
ORCH_PROJECT = "linear-orchestrator"
LEFTOVERS = {"__pycache__", ".DS_Store"}  # with *.egg-info, never a namespace_package entry
TYPE_HOMES = {
    "delegate.event.outcome.Outcome": "the event unit sits below the engine",
    "linear_orchestrator.linear.issues.Issue": "issues.py is its parser",
    "session.hosts.tables.Hosts": "hosts imports no session.engine module",
    **dict.fromkeys(
        (f"session.adapters.codex.sessions.{n}" for n in ("SessionFile", "SessionState")),
        "sessions.py runs by path as a stdlib-only script",
    ),
}
PROJECTS = {
    "delegate": "delegate",
    "session": SESSION,
    "utils": "utils",
    "manager": "manager",
    ORCH_PROJECT: ORCH,
}
LAYERS = ("utils", SESSION, "delegate", ORCH_PROJECT, "manager")
TOP = {"__init__.py", "cli.py", "engine"}
SUBPACKAGES = {
    SESSION: {"adapters", "hosts", "modes"},
    "delegate": {"composer", "event", "team"},
}
TOP_EXCEPTIONS = {
    SESSION: {"cli.py"},  # until TASK-369
    ORCH_PROJECT: {"cli.py", "linear"},  # until TASK-372
    "manager": {"cli.py", "crew.py", "roster.py"},  # until TASK-371
}
LOWER = tuple(p for p in PROJECTS if p != ORCH_PROJECT)
TREES = (*PROJECTS, "tests")
TESTS = ("tests", *(f"{p}/tests" for p in PROJECTS))
CONFIG_LEVELS = f"utils/src/{PKG}/utils/config_file.py"
LOCAL_EXAMPLE = re.compile(r"\s*# local: ?(.*)")
SESSION_SRC = f"session/src/{PKG}/{SESSION}/"
CONFIG_FILE = f"delegate/src/{PKG}/delegate/engine/config.py"
ADAPTERS_HOME = f"{SESSION_SRC}adapters/registry.py"
STATUS_LOG_HOME = f"{SESSION_SRC}engine/status_log.py"
STATUS_LOG_CLOCK = "%H:%M:%S"
# until TASK-368
STATUS_LOG_WRITERS = {TMUX_HOST}
COMMAND_SRC = (
    SESSION_SRC,
    f"delegate/src/{PKG}/delegate/",
    f"{ORCH_PROJECT}/{ORCH_SRC}",
)
MANAGER = "manager"
MANAGER_ALLOWED = {
    "manager.crew": ("utils", "manager.roster", "manager.engine.types"),
    "manager.roster": ("utils", "manager.engine.types"),
}
MANAGER_EXCEPTIONS = {"manager.crew": (f"{PKG}.{CONFIG}",)}  # until TASK-371
MANAGER_SESSION = ("manager.crew", "manager.roster")  # session names, until TASK-371
COMMAND = "agent-pm-"
SESSION_COMMAND = "agent-pm-session"
SPAWNS = re.compile(r"subprocess\.\w+|os\.(?:exec|spawn)\w*")
LINEAR = re.compile(f"{ORCH_PROJECT}|{ORCH}")
COMPOSER = f"{PKG}.delegate.composer"
EXPORTERS = (f"{PKG}.delegate", COMPOSER)
CONFIG_PY = f"{ORCH_SRC}engine/config.py"
TOOL_STRINGS = ("tmux", "adapters")
TOML_STRING = re.compile(r""""((?:\\.|[^"\\])*)"|'([^']*)'""")
ENTRIES = tuple(f"{ORCH_SRC}engine/{name}.py" for name in ("run", "promote", "router"))
ONCE = (f"{ORCH_PKG}.engine.config.load", f"{ORCH_PKG}.linear.linear.use_logs")
LOAD_HELPERS = ("_load_config", "_runnable", "_docs")
HANDOFF = re.compile(r"Handoff from|## (?:Source|Instructions|Comments)")
HANDOFF_HOME = f"{ORCH_SRC}linear/issues.py"
COMMAND_FILES = ("delegate/config.toml", f"{ORCH_PROJECT}/config.toml")
COMMAND_DOCS = ("delegate/skills/", "manager/skills/", f"delegate/src/{PKG}/delegate/team/")
PYTHON = re.compile(r"\bpython(?:3(?:\.\d+)?)?\b(?:[ \t]+[^\s`'\"]+){0,3}")
PYTHON_OK = re.compile(
    rf"\S+[ \t]+-P[ \t]+(?:-m[ \t]+{PKG}\.\w"
    r"|(?:<team>|\{\{team\}\})/tools/\w+\.py(?=[ \t]|\Z))"
)


def layer_imports(repo: str) -> list[str]:
    found = set()
    for rel, names, imports in _modules(repo):
        name = ".".join(names or ())
        own = _unit(name)
        own = own if own in UNITS else OUTSIDE
        for module, inner in imports:
            unit = _unit(inner) if inner else module.split(".")[0]
            if module.split(".")[0] in BARE:
                found.add(f"{rel}: bare import {module}")
            elif (
                unit in (TOOLS, ORCH)
                or (own != OUTSIDE and inner and unit not in (*UNITS, SESSION, *SHARED))
                or (_within(own, "session") and _within(unit, "delegate"))
                or (
                    unit in (*UNITS, SESSION)
                    and unit != own
                    and (own, unit) not in ALLOWED
                    and (name, unit) not in ALLOWED
                    and not (
                        own == OUTSIDE
                        and unit in SESSION_INTERNAL
                        and (names is None or _within(name, SESSION))
                    )
                )
            ):
                found.add(f"{rel}: {own} imports {module}")
    return sorted(found)


def delegate_session(repo: str) -> list[str]:
    found = set()
    exported = _all(os.path.join(repo, "session", "src", PKG, SESSION, "__init__.py"))
    for rel, names, imports in _modules(repo):
        if names and names[0] == "delegate" and not _within(".".join(names), CONFIG):
            found.update(
                f"{rel}: imports {m} outside session.__all__"
                for m, inner in imports
                if _within(inner, SESSION) and inner.partition(".")[2] not in exported
            )
    return sorted(found)


def layer_commands(repo: str) -> list[str]:
    found = set()
    for scanned in COMMAND_SRC:
        for rel, text in _files(os.path.join(repo, scanned), ".py"):
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


def standalone(repo: str) -> list[str]:
    found = set()
    for project in LOWER:
        for top, dirs, names in os.walk(os.path.join(repo, project)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            if top == os.path.join(repo, project):
                dirs[:] = [d for d in dirs if d != "tests"]
            for name in sorted(names):
                rel = os.path.relpath(os.path.join(top, name), repo).replace(os.sep, "/")
                with open(os.path.join(top, name), encoding="utf-8", errors="replace") as f:
                    for number, line in enumerate(f, 1):
                        if rel == CONFIG_LEVELS and line.startswith("LEVELS = "):
                            continue
                        found.update(f"{rel}:{number}: names {w}" for w in LINEAR.findall(line))
    return sorted(found)


def pure_utils(repo: str) -> list[str]:
    found = set()
    for rel, names, imports in _modules(repo):
        package = max((p for p in PURE if _within(".".join(names or ()), p)), key=len, default=None)
        if package:
            found.update(
                f"{rel}: {package} imports {m}"
                for m, inner in imports
                if not any(_within(inner, u) for u in PURE[package])
                and m.split(".")[0] not in sys.stdlib_module_names
            )
    return sorted(found)


def manager_imports(repo: str) -> list[str]:
    found = set()
    for rel, names, imports in _modules(repo, tuple(PROJECTS)):
        name = ".".join(names or ())
        allowed = MANAGER_ALLOWED.get(name, ("utils",))
        for m, inner in imports:
            if not _within(name, MANAGER):
                if _within(inner, MANAGER):
                    found.add(f"{rel}: imports {m}")
            elif not (
                m.split(".")[0] in sys.stdlib_module_names
                or any(_within(inner, u) for u in allowed)
                or m in MANAGER_EXCEPTIONS.get(name, ())
                or (name in MANAGER_SESSION and m.rpartition(".")[0] == f"{PKG}.{SESSION}")
            ):
                found.add(f"{rel}: {name} imports {m}")
    return sorted(found)


def package_top(repo: str) -> list[str]:
    found = set()
    for project in LAYERS[1:]:
        rel = f"{project}/src/{PKG}/{PROJECTS[project]}"
        path = os.path.join(repo, rel)
        have = {e for e in (os.listdir(path) if os.path.isdir(path) else ()) if e not in LEFTOVERS}
        exempt = TOP_EXCEPTIONS.get(project, set())
        found.update(f"{rel}/{e}: missing" for e in TOP - have - exempt)
        found.update(
            f"{rel}/{e}: not __init__.py, cli.py, engine/ or a listed subpackage"
            for e in have - TOP - SUBPACKAGES.get(project, set()) - exempt
        )
    return sorted(found)


def runtime_names(repo: str) -> list[str]:
    found = set()
    for rel, text in _lower_files(repo, (".py", ".toml"), skip=f"{SESSION_SRC}adapters/"):
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


def old_core(repo: str) -> list[str]:
    found = set()
    for rel, text in _tree_files(repo, ".py"):
        found.update(
            f"{rel}: imports {m}"
            for m in _imports(ast.parse(text, rel), None)
            if m.split(".")[0] == "core"
        )
    return sorted(found)


def session_only(repo: str) -> list[str]:
    found = set()
    for rel, names, imports in _modules(repo):
        name = ".".join(names or ())
        if _within(name, SESSION):
            found.update(
                f"{rel}: session imports {m}"
                for m, inner in imports
                if m.split(".")[0] not in sys.stdlib_module_names
                and not (_within(inner, SESSION) or _within(inner, "utils"))
            )
    for rel, text in _lower_files(repo, ".py"):
        if rel.startswith(SESSION_SRC):
            found.update(f"{rel}: names {w}" for w in SESSION_WORDS if w in text)
    return sorted(found)


def adapters_key(repo: str) -> list[str]:
    found = set()
    for rel, text in _lower_files(repo, ".py", skip=ADAPTERS_HOME):
        found.update(
            f'{rel}:{line}: literal key "adapters"'
            for line, s in _strings(ast.parse(text, rel))
            if s == "adapters"
        )
    return sorted(found)


def syntax_311(repo: str) -> list[str]:
    found = []
    for rel, text in _tree_files(repo, ".py"):
        try:
            ast.parse(text, rel, feature_version=(3, 11))
        except SyntaxError as e:
            found.append(f"{rel}: {e}")
    return sorted(found)


def modes_import_api(repo: str) -> list[str]:
    found = set()
    for rel, names, imports in _modules(repo):
        if names and _within(".".join(names), MODES):
            found.update(
                f"{rel}: imports {m}"
                for m, inner in imports
                if _within(inner, SESSION_API) or _within(inner, f"{SESSION}.api")
            )
    return sorted(found)


def tmux_calls(repo: str) -> list[str]:
    found = set()
    for rel, text in _lower_files(repo, ".py"):
        if rel != TMUX_HOST:
            found.update(
                f'{rel}:{line}: str constant "tmux"'
                for line, s in _strings(ast.parse(text, rel))
                if s == "tmux"
            )
    return sorted(found)


def status_log_format(repo: str) -> list[str]:
    found = set()
    for top in PROJECTS:
        for rel, text in _files(os.path.join(repo, top, "src"), ".py"):
            rel = f"{top}/src/{rel}"
            if rel != STATUS_LOG_HOME and rel not in STATUS_LOG_WRITERS:
                found.update(
                    f"{rel}:{line}: status log line format outside session/engine/status_log.py"
                    for line, s in _strings(ast.parse(text, rel))
                    if STATUS_LOG_CLOCK in s
                )
    return sorted(found)


def syntax_39(repo: str) -> list[str]:
    found = []
    for rel, text in _lower_files(repo, ".py"):
        if rel in PY39:
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


def import_hygiene(repo: str) -> list[str]:
    found = set()
    for rel, text in _tree_files(repo, ".py"):
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
    return sorted(found)


def namespace_package(repo: str) -> list[str]:
    found = set()
    for top, own in PROJECTS.items():
        for rel, wanted in (("src", {PKG}), (f"src/{PKG}", {own})):
            path = os.path.join(repo, top, rel)
            have = {
                e
                for e in (os.listdir(path) if os.path.isdir(path) else ())
                if e not in LEFTOVERS and not e.endswith(".egg-info")
            }
            found.update(
                f"{top}/{rel}/{e}: "
                + ("namespace package __init__.py" if e == "__init__.py" else f"not {top}'s")
                for e in have - wanted
            )
            found.update(f"{top}/{rel}/{e}: missing" for e in wanted - have)
    return sorted(found)


def shared_types(repo: str) -> list[str]:
    found = []
    for project in PROJECTS:
        src = os.path.join(repo, project, "src")
        trees = {
            rel[:-3].replace("/", ".").removesuffix(".__init__"): (rel, ast.parse(text, rel))
            for rel, text in _files(src, ".py")
        }
        data = {
            f"{module}.{n.name}"
            for module, (_, tree) in trees.items()
            for n in tree.body
            if isinstance(n, ast.ClassDef)
            and any("dataclass" in ast.unparse(d) for d in n.decorator_list)
        }
        for module, (rel, tree) in trees.items():
            if not rel.endswith("__init__.py"):
                found += _type_uses(project, rel, module, tree, data)
    return sorted(found)


def _type_uses(project, rel, module, tree, data):
    package, bound, used = module.split(".")[:-1], {}, []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source = ".".join(
                [*package[: len(package) - node.level + 1], *([node.module] if node.module else [])]
                if node.level
                else [node.module]
            )
            for a in node.names:
                bound[a.asname or a.name] = f"{source}.{a.name}"
                used.append((node.lineno, f"{source}.{a.name}"))
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0]
                )
    used += [(n.lineno, _dotted(n, bound)) for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    return sorted(
        {
            f"{project}/src/{rel}:{line}: {key}: a shared dataclass belongs in a types.py"
            for line, key in used
            if key in data
            and (source := key.rpartition(".")[0]) != module
            and not source.endswith(".types")
            and key.removeprefix(f"{PKG}.") not in TYPE_HOMES
        }
    )


def python_commands(repo: str) -> list[str]:
    found = set()
    for rel, text in _tree_files(repo, (".toml", ".md")):
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
        parents = {id(c): n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute):
                base = _dotted(n.value, bound)
                outer = n
                while isinstance(p := parents.get(id(outer)), ast.Attribute) and p.value is outer:
                    outer = p
                if (
                    base in exported
                    and n.attr not in exported[base]
                    and not _shared(_dotted(outer, bound))
                ):
                    found.add(f"{rel}:{n.lineno}: uses {base}.{n.attr}")
    return sorted(found)


def orchestrator_text(orch: str) -> list[str]:
    found = set()
    for rel, text in _files(orch, (".py", ".toml"), skip="tests/"):
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
    joined = f"{module}.{name}" if name else module
    if top in sys.stdlib_module_names or _within(joined, ORCH_PKG):
        return False
    if _within(joined, f"{PKG}.{CONFIG}"):
        return True
    packages = (PKG, *(f"{PKG}.{g}" for g in GROUPS))
    target = next(
        (t for t in (joined, module) if t in exported or _shared(t)),
        joined if module in packages else module,
    )
    if target in exported:
        return (
            module == target
            and name is not None
            and name not in exported[target]
            or (target == COMPOSER and rel != CONFIG_PY)
        )
    return not (
        _shared(target)
        or (module == f"{PKG}.{SESSION}" and name != "Session")  # both until TASK-372
        or (module, name, rel) == (f"{PKG}.{SESSION}", "Session", CONFIG_PY)
    )


def _shared(module):
    return any(_within(module, f"{PKG}.{s}") for s in SHARED)


def _exported(package):
    return _all(
        os.path.join(REPO, package.split(".")[1], "src", *package.split("."), "__init__.py")
    )


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


def test_layers(repo: str) -> list[str]:
    rank = {p: i for i, p in enumerate(LAYERS)}
    owner = {sub: project for project, sub in PROJECTS.items()}
    found = set()
    for project in LAYERS:
        for rel, text in _files(os.path.join(repo, project, "tests"), ".py"):
            rel = f"{project}/tests/{rel}"
            package = rel.split("/")[:-1]
            for m in _imports(ast.parse(text, rel), package, PKG):
                parts = m.split(".")
                top = owner.get(parts[1], "") if parts[0] == PKG and len(parts) > 1 else parts[0]
                if top == "tests" or rank.get(top, -1) > rank[project]:
                    found.add(f"{rel}: imports {m}")
    return sorted(found)


def config_levels(repo: str) -> list[str]:
    with open(os.path.join(repo, CONFIG_LEVELS), encoding="utf-8") as f:
        tree = ast.parse(f.read(), CONFIG_LEVELS)
    found = [
        n.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "LEVELS" for t in n.targets)
    ]
    if len(found) != 1:
        return [f"{CONFIG_LEVELS}: want one LEVELS assignment"]
    try:
        named = [p for level in ast.literal_eval(found[0]) for p in level]
    except (ValueError, TypeError):
        return [f"{CONFIG_LEVELS}: LEVELS is not a literal tuple of tuples"]
    return sorted(
        {
            f"{CONFIG_LEVELS}: LEVELS holds {p!r} {named.count(p)} times"
            for p in {*PROJECTS, *named}
            if named.count(p) != 1
        }
        | {
            f"{CONFIG_LEVELS}: LEVELS holds {p!r}, not a project"
            for p in named
            if p not in PROJECTS
        }
    )


def explicit_keys(repo: str) -> list[str]:
    real, examples, found = set(), {}, set()
    for project in PROJECTS:
        rel = f"{project}/config.toml"
        path = os.path.join(repo, rel)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        lines = [m[1] for line in text.splitlines() if (m := LOCAL_EXAMPLE.fullmatch(line))]
        try:
            real.update(_toml_leaves(tomllib.loads(text), ()))
            examples[rel] = _toml_leaves(tomllib.loads("\n".join(lines)), ())
        except tomllib.TOMLDecodeError as e:
            found.add(f"{rel}: {e}")
    keys = {k for k, empty in real}
    empty = {k for k, is_empty in real if is_empty}
    shapes = {(k[0], len(k), k[-1]) for k in keys}
    for rel, leaves in examples.items():
        for k, _ in leaves:
            if not (
                k in keys
                or (k[0], len(k), k[-1]) in shapes
                or any(k[i:] in keys for i in range(1, len(k)))
                or k[:-1] in empty
            ):
                found.add(f"{rel}: `# local:` key {'.'.join(k)} has no real line")
    return sorted(found)


def _toml_leaves(table, at):
    """(dotted key tuple, is an empty table) of each leaf."""
    out = [(at, True)] if not table and at else []
    for k, v in table.items():
        out += _toml_leaves(v, (*at, k)) if isinstance(v, dict) else [((*at, k), False)]
    return out


def _private(name):
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _modules(repo, projects=LOWER):
    for rel, text in _lower_files(repo, ".py", projects=projects):
        parts = rel[:-3].split("/")
        names = parts[3:] if parts[1:3] == ["src", PKG] else None
        imports = _imports(ast.parse(text, rel), [PKG, *names[:-1]] if names else None, PKG)
        yield rel, names, [(m, _inner(m, PKG)) for m in imports]


def _unit(inner):
    if _within(inner, HOST_ABC):
        return SESSION
    return max((u for u in ALL_UNITS if _within(inner, u)), key=len, default=inner.split(".")[0])


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


def _lower_files(repo, suffixes, skip=None, projects=LOWER):
    for project in projects:
        for rel, text in _files(os.path.join(repo, project), suffixes):
            rel = f"{project}/{rel}"
            if not rel.startswith(f"{project}/tests/") and not (skip and rel.startswith(skip)):
                yield rel, text


def _tree_files(repo, suffixes):
    for tree in TREES:
        for rel, text in _files(os.path.join(repo, tree), suffixes):
            yield f"{tree}/{rel}", text


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
