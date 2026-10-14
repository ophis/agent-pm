"""Repository design checks: each public function takes a repository root and returns violations."""

import ast
import builtins
import fnmatch
import importlib
import io
import os
import re
import sys
import tokenize
import tomllib

from agent_pm.utils.config_file import LEVELS

from tests.import_policy import violation

UNIT_ISOLATION_ALLOWED = {
    "session/tests/unit/adapters/codex/tui_test.py": "TASK-385 Phase B",
    "session/tests/unit/adapters/codex/tui/start_test.py": "TASK-385 Phase B",
    "session/tests/unit/engine/hosted_test.py": "TASK-385 Phase B",
    "session/tests/unit/fakes/host_test.py": "TASK-385 Phase B",
    "session/tests/unit/hosts/tmux/tmux_host_test.py": "TASK-385 Phase B",
    "session/tests/unit/modes/headless_test.py": "TASK-385 Phase B",
    "session/tests/unit/modes/tui_hook_test.py": "TASK-385 Phase B",
    "session/tests/unit/modes/tui_test.py": "TASK-385 Phase B",
}


RUNTIME = re.compile(r"claude|codex|print_prompt", re.I)


TOML_CODE = re.compile(r"""(?:[^#"']|"(?:\\.|[^"\\])*"|'[^']*')*""")


TOML_HEADER = re.compile(r"\s*\[\s*(?:adapters|policy)\.[a-z][a-z0-9_]*(\.[^\]]*)?\]\s*\Z")


TOML_IMPLEMENTATION = re.compile(
    r'\s*implementation\s*=\s*"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*"\s*\Z'
)


TOML_RUNTIME = re.compile(r'\s*runtime\s*=\s*"[a-z][a-z0-9_]*"\s*\Z')


TMUX_HOST = "session/src/agent_pm/session/hosts/tmux/tmux_host.py"


PKG = "agent_pm"


SESSION = "session"


ORCH = "linear_orchestrator"


PROJECTS = {p: p.replace("-", "_") for level in LEVELS for p in level}


LAYERS = tuple(PROJECTS)


ORCH_PKG = f"{PKG}.{ORCH}"


ORCH_SRC = f"src/{PKG}/{ORCH}/"


ORCH_PROJECT = "linear-orchestrator"


LEFTOVERS = {"__pycache__", ".DS_Store"}


TOP = {"__init__.py", "cli.py", "engine"}


SUBPACKAGES = {
    SESSION: {"adapters", "hosts", "modes"},
    "delegate": {"composer", "event", "team"},
    "linear-orchestrator": {"linear"},
}


ENTRY_MODULES = {SESSION: {"extension.py"}}  # public modules besides __init__.py


LOWER = tuple(p for p in PROJECTS if p != ORCH_PROJECT)


TREES = (*PROJECTS, "tests", ".github")


TESTS = ("tests", *(f"{p}/tests" for p in PROJECTS))


CONFIG_LEVELS = f"utils/src/{PKG}/utils/config_file.py"


LOCAL_EXAMPLE = re.compile(r"\s*# local: ?(.*)")


SESSION_SRC = f"session/src/{PKG}/{SESSION}/"


OBJECT_EXPORT_MODULES = (
    f"{SESSION_SRC}__init__.py",
    *(f"{SESSION_SRC}{module}" for module in sorted(ENTRY_MODULES[SESSION])),
)


OBJECT_EXPORTS_ALLOWED = {
    (f"{PKG}.{SESSION}", "read_config"): "TASK-389 layer-owned configuration entry point",
    (f"{PKG}.{SESSION}", "close"): "FR-4 forwarding wrapper; 356-14 (FR-27) deletes",
    (f"{PKG}.{SESSION}", "sessions"): "FR-4 forwarding wrapper; 356-14 (FR-27) deletes",
    (f"{PKG}.{SESSION}", "note"): "FR-4 forwarding wrapper; 356-14 (FR-27) deletes",
    (f"{PKG}.{SESSION}", "session_record"): "FR-4 forwarding wrapper; 356-14 (FR-27) deletes",
}


IMPORT_BOOTSTRAPS = {
    f"{SESSION_SRC}adapters/codex/tui/start.py": "_bootstrap",
    f"{SESSION_SRC}modes/tui_hook.py": "_bootstrap",
}


STANDALONE = frozenset(
    f"{SESSION_SRC}{path}"
    for path in (
        "hosts/tmux/tmux_host.py",
        "adapters/codex/common/app_server.py",
    )
)


PACKAGE_CLIMB_ALLOWED = {
    ("utils/src/agent_pm/utils/paths.py", "ROOT"): "TASK-393",
    ("utils/src/agent_pm/utils/paths.py", "import_roots"): "TASK-393",
    ("utils/src/agent_pm/utils/config_file.py", "read_project"): "TASK-393",
    (f"{SESSION_SRC}adapters/codex/tui/__init__.py", "import_roots()"): "TASK-393",
    ("delegate/src/agent_pm/delegate/engine/config.py", "DELEGATE"): "TASK-367",
    ("delegate/src/agent_pm/delegate/engine/config.py", "import_roots()"): "TASK-367",
    (f"{ORCH_PROJECT}/{ORCH_SRC}engine/config.py", "ROOT"): "TASK-372",
    (f"{ORCH_PROJECT}/{ORCH_SRC}engine/config.py", "import_roots()"): "TASK-372",
    ("manager/src/agent_pm/manager/engine/crew.py", "BIN"): "TASK-373",
}


SESSION_NAMES_ALLOWED = {
    (f"{SESSION_SRC}engine/config.py", "read_project"): "TASK-392",
}


NOQA_ALLOWED = {
    (
        f"{SESSION_SRC}modes/headless.py",
        "BLE001",
        "HeadlessMode._pump",
    ): "Forward failures to the caller thread.",
    (
        f"{SESSION_SRC}modes/in_process.py",
        "BLE001",
        "InProcessMode._pump",
    ): "Forward failures to the caller thread.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}linear/sessions.py",
        "BLE001",
        "_bounded.run",
    ): "Forward failures to the caller thread.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/promote.py",
        "BLE001",
        "Promoter.pending",
    ): "Keep reading the other handoffs.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/promote.py",
        "BLE001",
        "Promoter.run",
    ): "Keep processing other handoffs.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/promote.py",
        "BLE001",
        "run_prune",
    ): "Prune failures leave promotions intact.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/router.py",
        "BLE001",
        "start_run",
    ): "A recorded launch may fail.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/run.py",
        "BLE001",
        "inner",
    ): "Finish reporting a failed run.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}linear/writeback.py",
        "BLE001",
        "progress",
    ): "A sink failure must not stop the run.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}linear/writeback.py",
        "BLE001",
        "finish",
    ): "Report the session after failed steps.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}linear/writeback.py",
        "BLE001",
        "limit",
    ): "Report the session after a failed limit.",
    (
        f"{SESSION_SRC}modes/tui_hook.py",
        "BLE001",
        "__main__",
    ): "the hook must never fail the client runtime",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/router.py",
        "duplicate-code",
        "outer",
    ): "Build writeback.context directly in both callers; no shared builder by design.",
    (
        f"{ORCH_PROJECT}/{ORCH_SRC}engine/run.py",
        "duplicate-code",
        "inner",
    ): "Build writeback.context directly in both callers; no shared builder by design.",
}


ORCH_FILES = f"{ORCH_PROJECT}/{ORCH_SRC}"


OPTION_NAME = re.compile(r"_?[A-Z][A-Z0-9_]*")


OPTION_WORD = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")


OPTION_TUPLES_ALLOWED = {
    (CONFIG_LEVELS, "UNCONFIGURED"): "the projects without their own config.toml, a subset",
    (f"{SESSION_SRC}adapters/registry.py", "TUI_KEYS"): "the [hosts.tmux] keys tui() reads",
    (f"{SESSION_SRC}adapters/claude/tui.py", "MONTHS"): "month names, indexed by month",
    (f"{SESSION_SRC}adapters/claude/tui.py", "WEEKDAYS"): "weekday names, indexed by weekday",
    (TMUX_HOST, "GRID_HOOKS"): "tmux hook names the grid sets and unsets together",
    (
        f"delegate/src/{PKG}/delegate/event/outcome.py",
        "SAVES_DELIVERABLE",
    ): "the destinations whose deliverable comes back, a subset",
    (f"{ORCH_FILES}linear/linear.py", "ONCE"): "the log fields keying a once-only line",
    (f"{ORCH_FILES}engine/config.py", "CLONES"): "checkout dir names in a workdir",
    (f"{ORCH_FILES}engine/prune.py", "ARCHIVE_ROLES"): "roles whose finished issues are archived",
    (f"{ORCH_FILES}engine/router.py", "SHARED"): "the arguments router passes on to run",
}


CONFIG_FILE = f"delegate/src/{PKG}/delegate/engine/config.py"


ADAPTERS_HOME = f"{SESSION_SRC}adapters/registry.py"


STATUS_LOG_HOME = f"{SESSION_SRC}engine/status_log.py"


STATUS_LOG_CLOCK = "%H:%M:%S"


COMMAND_SRC = (
    SESSION_SRC,
    f"delegate/src/{PKG}/delegate/",
    f"{ORCH_PROJECT}/{ORCH_SRC}",
)


COMMAND = "agent-pm-"


SESSION_COMMAND = "agent-pm-session"


SPAWNS = re.compile(r"subprocess\.\w+|os\.(?:exec|spawn)\w*")


LINEAR = re.compile(f"{ORCH_PROJECT}|{ORCH}")


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
    f"\\S+[ \\t]+-P[ \\t]+(?:-m[ \\t]+{PKG}\\.\\w|(?:<team>|\\{{\\{{team\\}}\\}})/tools/\\w+\\.py(?=[ \\t]|\\Z))"
)


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
    aliases = _bindings(tree)
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
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and _boundary_name(node.value, aliases) == "sys.modules"
        ) or (
            isinstance(node, ast.Attribute)
            and _boundary_name(node.value, aliases) == "sys.modules"
            and node.attr
            in {"pop", "popitem", "update", "clear", "setdefault", "__setitem__", "__delitem__"}
        ):
            found.add((node.lineno, "sys.modules mutation"))
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
        "shipped code patch": call.endswith((".patch", ".patch.multiple"))
        and (_within(raw, "agent_pm") or _within(target, "agent_pm")),
        "shipped object patch": call.endswith(".patch.object") and _within(target, "agent_pm"),
        "sys.modules dictionary patch": call.endswith(".patch.dict") and target == "sys.modules",
        "shipped dictionary patch": call.endswith(".patch.dict") and _within(target, "agent_pm"),
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


def session_names(repo: str) -> list[str]:
    """Only engine/config.py forwards configuration reads; no parser, reply tag or team text."""
    forbidden = (
        "config_file",
        "tomllib",
        "agent_pm.delegate.engine.config",
        "read_project",
        "agent-pm-progress",
        "agent-pm-outcome",
        "team/",
    )
    return sorted(
        f"{SESSION_SRC}{rel}: names {word}"
        for rel, text in _files(os.path.join(repo, SESSION_SRC), ".py")
        for word in forbidden
        if word in text and (f"{SESSION_SRC}{rel}", word) not in SESSION_NAMES_ALLOWED
    )


def manager_commands(repo: str) -> list[str]:
    found = []
    rel = "manager/src/agent_pm/manager/engine/crew.py"
    path = os.path.join(repo, rel)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as source:
        tree = ast.parse(source.read(), rel)
    allowed = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple | ast.List):
            values = [v.value if isinstance(v, ast.Constant) else None for v in node.elts]
            if values == [SESSION_COMMAND, "ls", "--here", "--records"]:
                allowed.add(id(node.elts[0]))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith(COMMAND)
            and id(node) not in allowed
        ):
            found.append(f"{rel}:{node.lineno}: hardcoded layer command {node.value}")
    return sorted(found)


def manager_sid(repo: str) -> list[str]:
    rel = "manager/src/agent_pm/manager/engine/crew.py"
    path = os.path.join(repo, rel)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as source:
        tree = ast.parse(source.read(), rel)
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "getenv"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "AGENT_PM_SID"
        ) or (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "AGENT_PM_SID"
        ):
            found.append(f"{rel}:{node.lineno}: reads inherited AGENT_PM_SID")
    return sorted(found)


def manager_lease(repo: str) -> list[str]:
    rel = "manager/src/agent_pm/manager/engine/roster.py"
    path = os.path.join(repo, rel)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as source:
        tree = ast.parse(source.read(), rel)
    return sorted(
        f"{rel}:{node.lineno}: lease liveness callback"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "_alive"
    )


def manager_display(repo: str) -> list[str]:
    found = set()
    base = "manager/src/agent_pm/manager/"
    forbidden = re.compile(
        r"(?<![a-zA-Z0-9_])(?:pane|opener|split_from)(?![a-zA-Z0-9_])|--split|@record"
    )
    for rel, text in _files(os.path.join(repo, base), ".py"):
        tree = ast.parse(text, rel)
        for node in ast.walk(tree):
            value = (
                node.id
                if isinstance(node, ast.Name)
                else node.attr
                if isinstance(node, ast.Attribute)
                else node.arg
                if isinstance(node, ast.arg)
                else node.value
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                else ""
            )
            if forbidden.search(value):
                found.add(f"{base}{rel}:{node.lineno}: interprets display {value!r}")
    return sorted(found)


def manager_exports(repo: str) -> list[str]:
    base = "manager/src/agent_pm/manager/__init__.py"
    path = os.path.join(repo, base)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as source:
        exports = _all_node(ast.parse(source.read(), base))
    if exports is None:
        return [f"{base}: missing __all__"]
    try:
        names = ast.literal_eval(exports)
    except (ValueError, TypeError):
        return [f"{base}:{exports.lineno}: __all__ must be a literal sequence of names"]
    if not isinstance(names, list | tuple) or not all(isinstance(name, str) for name in names):
        return [f"{base}:{exports.lineno}: __all__ must be a literal sequence of names"]
    found = []
    for name, (_, kind) in _export_kinds(repo, "agent_pm.manager", names, "manager").items():
        if kind == "function":
            found.append(f"{base}:{exports.lineno}: exports function {name}")
        elif kind not in ("class", "constant") and (not name.isupper() or kind == "module"):
            found.append(f"{base}:{exports.lineno}: exports nonconstant {name}")
    return sorted(found)


def object_exports(repo: str) -> list[str]:
    """FR-44: a session entry module's literal __all__ exports only classes, constants and type
    aliases, unless allowlisted.
    """
    found, seen = set(), set()
    for rel in OBJECT_EXPORT_MODULES:
        path = os.path.join(repo, rel)
        if not os.path.isfile(path):
            continue
        package = rel.removeprefix("session/src/").removesuffix(".py").replace("/", ".")
        package = package.removesuffix(".__init__")
        with open(path, encoding="utf-8") as source:
            exports = _all_node(ast.parse(source.read(), rel))
        literal = isinstance(exports, ast.List) and _is_literal(exports)
        names = ast.literal_eval(exports) if literal else None
        if names is None or not all(isinstance(name, str) for name in names):
            found.add(f"{rel}: no literal __all__")
            continue
        for name, (_, kind) in _export_kinds(repo, package, names, SESSION).items():
            if kind in ("class", "constant", "alias"):
                continue
            if (package, name) in OBJECT_EXPORTS_ALLOWED:
                seen.add((package, name))
            elif kind == "value":
                found.add(f"{rel}: __all__ exports unrecognised value {name} (FR-44)")
            else:
                found.add(f"{rel}: __all__ exports function {name} (FR-44)")
        found.update(
            f"{pkg}: {name}: allowlisted but not an exported function (FR-44)"
            for pkg, name in OBJECT_EXPORTS_ALLOWED.keys() - seen
            if pkg == package
        )
    return sorted(found)


def _all_node(tree):
    return next(
        (
            n.value
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets)
        ),
        None,
    )


def _is_literal(node):
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError):
        return False
    return True


def _export_kinds(repo, package, names, project):
    """Each name `package` exports: what it resolves to through re-exports and aliases, and that
    target's kind (function, class, constant, alias, value, module or None: unresolved).
    """
    projects = (project, "utils")
    modules = _source_modules(repo, projects)
    definitions = _definitions(repo, projects)
    kinds = {}
    for name in names:
        original = f"{package}.{name}"
        resolved = _resolve_boundary(original, modules)
        kind = definitions.get(original) or definitions.get(resolved)
        if kind is None and "." not in resolved:
            kind = definitions.get(f"{package}.{resolved}")
        if kind is None:
            module, _, member = resolved.rpartition(".")
            if module.split(".")[0] in sys.stdlib_module_names:
                value = getattr(importlib.import_module(module), member, None)
                kind = (
                    "class" if isinstance(value, type) else "function" if callable(value) else None
                )
        if resolved in modules or resolved in sys.stdlib_module_names:
            kind = "module"
        kinds[name] = (resolved, kind)
    return kinds


def _definitions(repo, projects):
    """module.name: the kind of each top-level def, class or assignment (an alias resolves later)."""
    found = {}
    for project in projects:
        for rel, text in _files(os.path.join(repo, project, "src"), ".py"):
            module = rel.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            for node in ast.parse(text).body:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    found[f"{module}.{node.name}"] = "function"
                elif isinstance(node, ast.ClassDef):
                    found[f"{module}.{node.name}"] = "class"
                elif isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
                    if isinstance(node.value, ast.Name | ast.Attribute):
                        continue
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            found[f"{module}.{target.id}"] = _assigned_kind(node, target.id)
    return found


GENERICS = frozenset(
    {"Annotated", "Callable", "Iterable", "Iterator", "Literal", "Mapping", "Optional", "Sequence"}
    | {"Union", "dict", "frozenset", "list", "set", "tuple", "type"}
)


def _assigned_kind(node, name):
    """function (a lambda); constant (a literal, or an ALL_CAPS tuple, list or set of literals and
    dotted names); alias (annotated TypeAlias, a union or a subscripted generic); else value.
    """
    value = node.value
    if isinstance(value, ast.Lambda):
        return "function"
    if _is_literal(value) or name.isupper() and _references(value):
        return "constant"
    if _name(getattr(node, "annotation", None)).endswith("TypeAlias") or _typing(value):
        return "alias"
    return "value"


def _references(node):
    return isinstance(node, ast.Tuple | ast.List | ast.Set) and all(
        _is_literal(e) or _name(e) for e in node.elts
    )


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (base := _name(node.value)):
        return f"{base}.{node.attr}"
    return ""


def _typing(node, *, operand=False):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _typing(node.left, operand=True) and _typing(node.right, operand=True)
    if isinstance(node, ast.Subscript):
        return _name(node.value).rpartition(".")[2] in GENERICS
    return operand and (bool(_name(node)) or isinstance(node, ast.Constant) and node.value is None)


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


def source_suppressions(repo: str) -> list[str]:
    """Suppressions in every code tree need an exact inventory entry, reason and one use."""
    found, seen = [], set()
    for path, text in _tree_files(repo, ".py"):
        functions = list(_suppression_functions(ast.parse(text)))
        disabled = {}
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT or not re.search(
                r"#\s*(?:(?:ruff|flake8)\s*:\s*noqa\b|noqa\b|type\s*:\s*ignore\b|(?:pyright|pylint|isort)\s*:)",
                token.string,
                re.I,
            ):
                continue
            at = f"{path}:{token.start[0]}"
            scope = next(
                (
                    name
                    for first, last, name in reversed(functions)
                    if first <= token.start[0] <= last
                ),
                "<module>",
            )
            pylint = re.fullmatch(
                r"# pylint: (disable|enable)=duplicate-code(?:\s+#\s*(.+?))?\s*",
                token.string,
            )
            if pylint:
                key = (path, "duplicate-code", scope)
                detail = f"suppression duplicate-code in {scope}"
                if pylint[1] == "enable":
                    if key not in disabled or disabled[key] != token.start[1] or pylint[2]:
                        found.append(f"{at}: {detail} has no matching disable")
                    else:
                        del disabled[key]
                elif key not in NOQA_ALLOWED:
                    found.append(f"{at}: {detail} is not allowed")
                elif pylint[2] != NOQA_ALLOWED[key]:
                    found.append(f"{at}: {detail} reason differs from NOQA_ALLOWED")
                elif key in seen:
                    found.append(f"{at}: duplicate {detail}")
                else:
                    disabled[key] = token.start[1]
                    seen.add(key)
                continue
            match = re.fullmatch(
                r"#\s*noqa\s*:\s*([A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*)\s*#\s*(.+?)\s*",
                token.string,
                re.I,
            )
            if not match:
                found.append(f"{at}: suppression needs explicit codes and a recorded reason")
                continue
            for code in re.split(r"\s*,\s*", match[1].upper()):
                key = (path, code, scope)
                detail = f"suppression {code} in {scope}"
                if key not in NOQA_ALLOWED:
                    found.append(f"{at}: {detail} is not allowed")
                elif match[2] != NOQA_ALLOWED[key]:
                    found.append(f"{at}: {detail} reason differs from NOQA_ALLOWED")
                elif key in seen:
                    found.append(f"{at}: duplicate {detail}")
                else:
                    seen.add(key)
        found.extend(
            f"{path}: suppression {code} in {scope} has no matching enable"
            for _, code, scope in disabled
        )
    found.extend(
        f"{path}: suppression {code} in {scope} has no matching comment"
        for path, code, scope in sorted(NOQA_ALLOWED.keys() - seen)
    )
    return found


def _suppression_functions(tree, parents=()):
    for node in ast.iter_child_nodes(tree):
        scope = parents
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (*parents, node.name)
            if not isinstance(node, ast.ClassDef):
                yield node.lineno, node.end_lineno, ".".join(scope)
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.Eq)
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "__main__"
        ):
            scope = (*parents, "__main__")
            yield node.lineno, node.end_lineno, ".".join(scope)
        yield from _suppression_functions(node, scope)


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


def package_top(repo: str) -> list[str]:
    found = set()
    for project in (p for p in PROJECTS if p != "utils"):
        rel = f"{project}/src/{PKG}/{PROJECTS[project]}"
        path = os.path.join(repo, rel)
        have = {e for e in (os.listdir(path) if os.path.isdir(path) else ()) if e not in LEFTOVERS}
        found.update(f"{rel}/{e}: missing" for e in (TOP - {"cli.py"}) - have)
        listed = TOP | SUBPACKAGES.get(project, set()) | ENTRY_MODULES.get(project, set())
        found.update(
            f"{rel}/{e}: not __init__.py, cli.py, engine/, a listed subpackage or a listed entry"
            " module"
            for e in have - listed
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


def adapters_key(repo: str) -> list[str]:
    found = set()
    for rel, text in _lower_files(repo, ".py", skip=ADAPTERS_HOME):
        found.update(
            f'{rel}:{line}: literal key "adapters"'
            for line, s in _strings(ast.parse(text, rel))
            if s == "adapters"
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
            if rel not in (STATUS_LOG_HOME, TMUX_HOST):
                found.update(
                    f"{rel}:{line}: status log line format outside session/engine/status_log.py or hosts/tmux/tmux_host.py"
                    for line, s in _strings(ast.parse(text, rel))
                    if STATUS_LOG_CLOCK in s
                )
    return sorted(found)


def private_imports(root: str) -> list[str]:
    """Ruff 0.16.9 PLC2701 is preview; this guard supplies stable private-import checks."""
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


def unit_test_modules(repo: str) -> list[str]:
    """Unit <rel>/<name>_test.py matches a source module or nonempty directory at the same path, or
    a helper module under the project's tests. Package tests use the package name, including the
    layer package at unit root. Integration tests and non-_test.py helpers are outside this rule.
    """
    found = []
    for project, layer in PROJECTS.items():
        tests = os.path.join(repo, project, "tests")
        for top, dirs, names in os.walk(tests):
            parts = os.path.relpath(top, tests).split(os.sep)
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            if "unit" not in parts:
                dirs[:] = [d for d in dirs if d != "integration"]
                continue
            unit = parts.index("unit")
            relative = parts[unit + 1 :]
            for name in sorted(n for n in names if n.endswith("_test.py")):
                stem = name.removesuffix("_test.py")
                source = os.path.join(project, "src", PKG, layer, *relative, stem)
                home = os.path.join(repo, source)
                helper = os.path.join(tests, *parts[:unit], *relative, stem + ".py")
                matches = (
                    os.path.isfile(home + ".py")
                    or os.path.isfile(os.path.join(home, "__init__.py"))
                    or any(files for _, _, files in os.walk(home))
                    or os.path.isfile(helper)
                    or (
                        not relative
                        and stem == layer
                        and os.path.isfile(
                            os.path.join(repo, project, "src", PKG, layer, "__init__.py")
                        )
                    )
                )
                if not stem or stem == "__init__" or not matches:
                    test = os.path.relpath(os.path.join(top, name), repo)
                    found.append(f"{test}: missing source module {source}.py")
    return sorted(found)


def unit_isolation_allowlist(repo: str) -> list[str]:
    return [
        f"{path}: missing unit isolation exception"
        for path in sorted(UNIT_ISOLATION_ALLOWED)
        if not os.path.isfile(os.path.join(repo, path))
    ]


def shared_types(repo: str) -> list[str]:
    found = []
    for project in PROJECTS:
        src = os.path.join(repo, project, "src")
        trees = {
            rel[:-3].replace("/", ".").removesuffix(".__init__"): (rel, ast.parse(text, rel))
            for rel, text in _files(src, ".py")
            if f"{project}/src/{rel}" not in STANDALONE
        }
        data = {
            f"{module}.{node.name}": rel.endswith("__init__.py")
            for module, (rel, tree) in trees.items()
            for bound in (_bindings(tree),)
            for node in tree.body
            if isinstance(node, ast.ClassDef) and _data_type(node, bound)
        }
        for module, (rel, tree) in trees.items():
            if not rel.endswith("__init__.py"):
                found += _type_uses(project, rel, module, tree, data)
    return sorted(found)


def _data_type(node, bound):
    return any(
        _dotted(decorator.func if isinstance(decorator, ast.Call) else decorator, bound)
        == "dataclasses.dataclass"
        for decorator in node.decorator_list
    ) or any(
        _dotted(base, bound)
        in {"typing.NamedTuple", "enum.Enum", "enum.StrEnum", "typing.TypedDict"}
        for base in node.bases
    )


def types_only(repo: str) -> list[str]:
    found, private = [], {}
    for project in PROJECTS:
        src = os.path.join(repo, project, "src")
        for rel, text in _files(src, ".py"):
            if not (rel.endswith("/types.py") or "/types/" in rel):
                continue
            tree = ast.parse(text, rel)
            bound = _bindings(tree)
            kinds = _type_kinds(tree, bound)
            for node in _module_nodes(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    if _private(node.name):
                        module = rel.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
                        private[f"{module}.{node.name}"] = f"{project}/src/{rel}"
                    else:
                        found.append(
                            f"{project}/src/{rel}:{node.lineno}: types module function {node.name}"
                        )
                elif isinstance(node, ast.ClassDef):
                    kind = kinds.get(node.name)
                    if kind is None:
                        found.append(
                            f"{project}/src/{rel}:{node.lineno}: {node.name}: not a type class"
                        )
                    else:
                        found += _type_methods(project, rel, node, kind, bound)
    return sorted([*found, *_types_private_uses(repo, private)])


def _types_private_uses(repo, private):
    found = set()
    modules = _source_modules(repo, PROJECTS)
    for rel, text in _tree_files(repo, ".py"):
        tree = ast.parse(text, rel)
        bound = _bindings(tree)
        uses = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                uses.extend((node.lineno, f"{node.module}.{a.name}") for a in node.names)
            elif isinstance(node, ast.Name | ast.Attribute):
                uses.append((node.lineno, _dotted(node, bound)))
        for line, name in uses:
            name = _resolve_boundary(name, modules)
            if name in private and rel != private[name]:
                found.add(f"{rel}:{line}: private types helper {name} used outside its module")
    return found


def _module_nodes(tree):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield node
        else:
            yield from _module_nodes(node)


def _type_kinds(tree, bound):
    classes = [node for node in _module_nodes(tree) if isinstance(node, ast.ClassDef)]
    kinds = {}
    bases = {
        "typing.NamedTuple": "data",
        "typing.TypedDict": "data",
        "enum.Enum": "data",
        "enum.StrEnum": "data",
        "typing.Protocol": "protocol",
        "abc.ABC": "abstract",
    }
    for _ in classes:
        for node in classes:
            if any(
                _dotted(d.func if isinstance(d, ast.Call) else d, bound) == "dataclasses.dataclass"
                for d in node.decorator_list
            ):
                kinds[node.name] = "dataclass"
            else:
                for base in node.bases:
                    name = _dotted(base, bound)
                    kind = bases.get(name) or kinds.get(name)
                    if _exception_type(name):
                        kind = "exception"
                    if kind:
                        kinds[node.name] = kind
                        break
    return kinds


def _exception_type(name):
    module, _, attr = name.rpartition(".")
    if not module or module == "builtins":
        value = getattr(builtins, attr or name, None)
    elif module.split(".")[0] in sys.stdlib_module_names:
        value = getattr(importlib.import_module(module), attr, None)
    else:
        return False
    return isinstance(value, type) and issubclass(value, BaseException)


def _type_methods(project, rel, node, kind, bound):
    found = []
    fields = {
        child.target.id
        for child in node.body
        if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
    }
    methods = {
        child.name
        for child in node.body
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for method in (
        child for child in node.body if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
    ):
        decorators = {
            _dotted(d.func if isinstance(d, ast.Call) else d, bound) for d in method.decorator_list
        }
        if (
            method.name.startswith("__")
            and method.name.endswith("__")
            or decorators & {"property", "abc.abstractmethod"}
        ):
            continue
        if kind == "protocol" and all(
            isinstance(child, ast.Pass)
            or isinstance(child, ast.Expr)
            and isinstance(child.value, ast.Constant)
            for child in method.body
        ):
            continue
        args = [*method.args.posonlyargs, *method.args.args]
        own = [
            child.attr
            for child in ast.walk(method)
            if isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and args
            and child.value.id == args[0].arg
        ]
        if (
            kind == "dataclass"
            and own
            and set(own) <= fields | methods
            and not decorators & {"staticmethod", "classmethod"}
        ):
            continue
        if kind == "dataclass" and "classmethod" in decorators and _builds(method, args):
            continue
        found.append(
            f"{project}/src/{rel}:{method.lineno}: {node.name}.{method.name}: business method in types module"
        )
    return found


def _builds(method, args):
    """Whether a classmethod calls its own class: an alternate constructor."""
    return bool(args) and any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == args[0].arg
        for child in ast.walk(method)
    )


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
            f"{project}/src/{rel}:{line}: {key}: a shared data type belongs in a types.py"
            for line, key in used
            if key in data
            and (source := key.rpartition(".")[0]) != module
            and not source.endswith(".types")
            and not (data[key] and module.startswith(source + "."))
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


def orchestrator_text(repo: str) -> list[str]:
    found = set()
    for rel, text in _files(os.path.join(repo, ORCH_PROJECT), (".py", ".toml"), skip="tests/"):
        lines = text.splitlines()
        grammar = rel.startswith("src/") and rel.endswith(".py") and rel != HANDOFF_HOME
        for number, line in enumerate(lines, 1):
            found.update(
                f"{rel}:{number}: runtime name {w}"
                for w in RUNTIME.findall(line.replace("AGENTS.md", " "))
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


def config_once(repo: str) -> list[str]:
    found = set()
    for rel, text in _files(os.path.join(repo, ORCH_PROJECT), ".py"):
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
        for key, _ in leaves:
            k = key[1:] if key[0] in PROJECTS.values() else key
            if not k:
                continue
            if not (
                k in keys
                or (k[0], len(k), k[-1]) in shapes
                or any(k[i:] in keys for i in range(1, len(k)))
                or k[:-1] in empty
            ):
                found.add(f"{rel}: `# local:` key {'.'.join(key)} has no real line")
    return sorted(found)


def _toml_leaves(table, at):
    """(dotted key tuple, is an empty table) of each leaf."""
    out = [(at, True)] if not table and at else []
    for k, v in table.items():
        out += _toml_leaves(v, (*at, k)) if isinstance(v, dict) else [((*at, k), False)]
    return out


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


def test_boundaries(repo: str) -> list[str]:
    modules = _source_modules(repo, PROJECTS)
    return sorted(
        f"{rel}:{line}: {reason}"
        for rel, text in _tree_files(repo, ".py")
        for line, reason in _boundary_violations(
            text, rel.split("/")[0].replace("-", "_"), modules, shipped="/src/" in rel
        )
    )


def option_tuples(repo: str) -> list[str]:
    """FR-49: a module-level tuple of option words in shipped code is a StrEnum, unless allowlisted."""
    found, seen = set(), set()
    for project in PROJECTS:
        for rel, text in _files(os.path.join(repo, project, "src"), ".py"):
            rel = f"{project}/src/{rel}"
            for node in _module_assignments(ast.parse(text, rel).body):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for name in (t.id for t in targets if isinstance(t, ast.Name)):
                    if not (OPTION_NAME.fullmatch(name) and _option_words(node.value)):
                        continue
                    if (rel, name) in OPTION_TUPLES_ALLOWED:
                        seen.add((rel, name))
                    else:
                        found.add(
                            f"{rel}:{node.lineno}: {name}: a fixed option set is a StrEnum (FR-49)"
                        )
    found.update(
        f"{rel}: {name}: allowlisted but not an option tuple"
        for rel, name in OPTION_TUPLES_ALLOWED.keys() - seen
    )
    return sorted(found)


def _module_assignments(body):
    for node in body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            yield node
        elif isinstance(node, ast.If | ast.Try):
            blocks = [node.body, node.orelse, *(h.body for h in getattr(node, "handlers", ()))]
            for block in [*blocks, getattr(node, "finalbody", [])]:
                yield from _module_assignments(block)


def _option_words(value):
    return (
        isinstance(value, ast.Tuple)
        and len(value.elts) >= 2
        and all(
            isinstance(e, ast.Constant)
            and isinstance(e.value, str)
            and OPTION_WORD.fullmatch(e.value)
            for e in value.elts
        )
    )


def package_climbs(repo: str) -> list[str]:
    """Runtime paths stay inside their own package; exemptions expire with their last use."""
    found, seen = [], set()
    for project, package in PROJECTS.items():
        base = f"{project}/src/agent_pm/{package}"
        for sub, text in _files(os.path.join(repo, base), (".py",)):
            rel = f"{base}/{sub}"
            depth = len(sub.split("/"))
            tree = ast.parse(text, rel)
            bound = _bindings(tree)
            paths = dict.fromkeys(("agent_pm.utils.ROOT", "agent_pm.utils.paths.ROOT"), depth + 1)
            paths.update({name: depth + 1 for name, target in bound.items() if target in paths})
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in (
                    "agent_pm.utils",
                    "agent_pm.utils.paths",
                ):
                    if node.module.startswith(f"agent_pm.{package}."):
                        continue
                    for alias in node.names:
                        name = "import_roots()" if alias.name == "import_roots" else alias.name
                        used = any(
                            isinstance(use, ast.Name)
                            and isinstance(use.ctx, ast.Load)
                            and use.id == (alias.asname or alias.name)
                            for use in ast.walk(tree)
                        )
                        if alias.name in ("ROOT", "import_roots") and not used:
                            found.append(f"{rel}:{node.lineno}: {name}: path escapes its package")
            for name, line in _package_paths(tree, bound, paths, depth, project):
                key = rel, name
                if key in PACKAGE_CLIMB_ALLOWED:
                    seen.add(key)
                else:
                    found.append(f"{rel}:{line}: {name}: path escapes its package")
    found.extend(
        f"{rel}: {name}: allowlisted but no package escape ({ticket})"
        for (rel, name), ticket in PACKAGE_CLIMB_ALLOWED.items()
        if (rel, name) not in seen
    )
    return sorted(set(found))


def _package_paths(node, bound, paths, depth, project, *, scope=""):
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        paths = dict(paths)
        args = [*node.args.posonlyargs, *node.args.args]
        defaults = [
            *zip(args[len(args) - len(node.args.defaults) :], node.args.defaults, strict=True),
            *zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True),
        ]
        for arg, default in defaults:
            climb = _package_climb(default, bound, paths)
            if climb is not None:
                paths[arg.arg] = climb
                if climb > depth and isinstance(default, ast.Name) and default.id in bound:
                    yield node.name, default.lineno
        scope = node.name
    if isinstance(node, ast.Assign | ast.AnnAssign):
        value = node.value
        climb = _package_climb(value, bound, paths)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and climb is not None:
                paths[target.id] = climb
                if climb > depth and _adds_climb(value, bound):
                    yield scope or target.id, node.lineno
        scope = scope or next((t.id for t in targets if isinstance(t, ast.Name)), "")
    name = _dotted(node, bound)
    if isinstance(node, ast.Name | ast.Attribute) and isinstance(node.ctx, ast.Load):
        if name in ("agent_pm.utils.ROOT", "agent_pm.utils.paths.ROOT"):
            yield scope or "ROOT", node.lineno
        if name in ("agent_pm.utils.import_roots", "agent_pm.utils.paths.import_roots"):
            yield "import_roots()", node.lineno
    if isinstance(node, ast.Call):
        name = _dotted(node.func, bound)
        if name == "os.listdir" and node.args:
            climb = _package_climb(node.args[0], bound, paths)
            if climb is not None and climb > depth:
                yield scope, node.lineno
    if isinstance(node, ast.Call | ast.BinOp):
        parts = node.args[1:] if isinstance(node, ast.Call) else [node.right]
        is_join = (
            _dotted(node.func, bound) == "os.path.join"
            if isinstance(node, ast.Call)
            else isinstance(node.op, ast.Div)
        )
        base = (
            node.args[0]
            if isinstance(node, ast.Call) and node.args
            else node.left
            if isinstance(node, ast.BinOp)
            else None
        )
        if (
            is_join
            and (climb := _package_climb(base, bound, paths)) is not None
            and climb > depth
            and any(
                isinstance(part, ast.Constant)
                and isinstance(part.value, str)
                and set(part.value.split("/")) & (PROJECTS.keys() - {project})
                for part in parts
            )
        ):
            yield scope, node.lineno
    if _adds_climb(node, bound):
        climb = _package_climb(node, bound, paths)
        if climb is not None and climb > depth:
            yield scope, node.lineno
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and not (
            isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)
            and node.slice.value >= 0
        )
        and _package_climb(node.value.value, bound, paths) is not None
    ):
        yield scope, node.lineno
    for child in ast.iter_child_nodes(node):
        yield from _package_paths(child, bound, paths, depth, project, scope=scope)


def _adds_climb(node, bound):
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and _path_steps(node.right, bound) > 0
        or isinstance(node, ast.Attribute)
        and node.attr == "parent"
        or isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        or isinstance(node, ast.Call)
        and (
            _dotted(node.func, bound) == "os.path.dirname"
            or _dotted(node.func, bound) == "os.path.join"
            and any(_path_steps(a, bound) > 0 for a in node.args[1:])
        )
    )


def _package_climb(node, bound, paths):
    if (name := _dotted(node, bound)) in ("agent_pm.utils.ROOT", "agent_pm.utils.paths.ROOT"):
        return paths.get(name)
    if isinstance(node, ast.Name):
        return 0 if node.id == "__file__" else paths.get(node.id)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _package_climb(node.value, bound, paths)
        return None if base is None else base + 1
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        base = _package_climb(node.value.value, bound, paths)
        return None if base is None else base + node.slice.value + 1
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _package_climb(node.left, bound, paths)
        return None if base is None else base + _path_steps(node.right, bound)
    if not isinstance(node, ast.Call):
        return None
    name = _dotted(node.func, bound)
    if name in ("os.path.dirname", "os.path.join") and node.args:
        base = _package_climb(node.args[0], bound, paths)
        return (
            None
            if base is None
            else base
            + (1 if name.endswith("dirname") else sum(_path_steps(a, bound) for a in node.args[1:]))
        )
    if name in ("pathlib.Path", "os.path.abspath", "os.path.realpath", "str") and node.args:
        return _package_climb(node.args[0], bound, paths)
    if isinstance(node.func, ast.Attribute) and node.func.attr in ("resolve", "absolute"):
        return _package_climb(node.func.value, bound, paths)
    return None


def _path_steps(node, bound):
    if isinstance(node, ast.Starred):
        return _path_steps(node.value, bound)
    if isinstance(node, ast.List | ast.Tuple):
        return sum(_path_steps(v, bound) for v in node.elts)
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult)
        and isinstance(node.right, ast.Constant)
        and isinstance(node.right.value, int)
    ):
        return _path_steps(node.left, bound) * node.right.value
    if _dotted(node, bound) == "os.pardir":
        return 1
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return sum(
            1 if part == ".." else -1 for part in node.value.split("/") if part not in ("", ".")
        )
    return 0
