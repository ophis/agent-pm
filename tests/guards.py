"""Repository design checks: each public function takes a repository root and returns violations."""

import ast
import io
import os
import re
import tokenize
import tomllib

from agent_pm.utils.config_file import LEVELS

RUNTIME = re.compile(r"claude|codex|print_prompt", re.I)


TOML_CODE = re.compile(r"""(?:[^#"']|"(?:\\.|[^"\\])*"|'[^']*')*""")


TOML_HEADER = re.compile(r"\s*\[\s*(?:adapters|policy)\.[a-z][a-z0-9_]*(\.[^\]]*)?\]\s*\Z")


TOML_IMPLEMENTATION = re.compile(
    r'\s*implementation\s*=\s*"adapters\.[a-z][a-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*Adapter"\s*\Z'
)


TOML_RUNTIME = re.compile(r'\s*runtime\s*=\s*"[a-z][a-z0-9_]*"\s*\Z')


TMUX_HOME = "session/src/agent_pm/session/hosts/tmux/"


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


LOWER = tuple(p for p in PROJECTS if p != ORCH_PROJECT)


TREES = (*PROJECTS, "tests", ".github")


TESTS = ("tests", *(f"{p}/tests" for p in PROJECTS))


CONFIG_LEVELS = f"utils/src/{PKG}/utils/config_file.py"


LOCAL_EXAMPLE = re.compile(r"\s*# local: ?(.*)")


SESSION_SRC = f"session/src/{PKG}/{SESSION}/"


CONFIG_FILE = f"delegate/src/{PKG}/delegate/engine/config.py"


ADAPTERS_HOME = f"{SESSION_SRC}adapters/types.py"


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
    rf"\S+[ \t]+-P[ \t]+(?:-m[ \t]+{PKG}\.\w"
    r"|(?:<team>|\{\{team\}\})/tools/\w+\.py(?=[ \t]|\Z))"
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


def _comment_blocks(text):
    lines = text.splitlines()
    blocks, current = [], []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT or lines[token.start[0] - 1][: token.start[1]].strip():
            continue
        line, indent = token.start
        if current and (
            line != current[-1][0] + 1 or indent != lines[current[-1][0] - 1].index("#")
        ):
            blocks.append(current)
            current = []
        content = token.string[1:].removeprefix(" ")
        current.append((line, content.strip(), indent + len(content) - len(content.lstrip())))
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
                (
                    line,
                    lines[line - 1].strip(),
                    len(lines[line - 1]) - len(lines[line - 1].lstrip()),
                )
                for line in range(value.lineno, value.end_lineno + 1)
            ]
            indent = min((entry[2] for entry in entries[1:] if entry[1]), default=value.col_offset)
            entries[0] = (entries[0][0], entries[0][1].lstrip("rRuU")[3:], indent)
            entries[-1] = (entries[-1][0], entries[-1][1][:-3], entries[-1][2])
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


def _prose_pairs(block):
    fenced, previous = False, None
    indent = min((entry[2] for entry in block), default=0)
    for entry in block:
        _, body, column = entry
        if body.startswith(("```", "~~~")):
            fenced, previous = not fenced, None
            continue
        if fenced or column >= indent + 4 or body.startswith((">>>", "...")):
            previous = None
            continue
        if previous and previous[1] and body and not re.match(r"(?:[-*]|\d+[.)])\s", body):
            yield previous, entry
        previous = entry if body else None


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


def package_top(repo: str) -> list[str]:
    found = set()
    for project in (p for p in PROJECTS if p != "utils"):
        rel = f"{project}/src/{PKG}/{PROJECTS[project]}"
        path = os.path.join(repo, rel)
        have = {e for e in (os.listdir(path) if os.path.isdir(path) else ()) if e not in LEFTOVERS}
        found.update(f"{rel}/{e}: missing" for e in (TOP - {"cli.py"}) - have)
        found.update(
            f"{rel}/{e}: not __init__.py, cli.py, engine/ or a listed subpackage"
            for e in have - TOP - SUBPACKAGES.get(project, set())
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
        if not rel.startswith(TMUX_HOME):
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
            if rel != STATUS_LOG_HOME and not rel.startswith(TMUX_HOME):
                found.update(
                    f"{rel}:{line}: status log line format outside "
                    "session/engine/status_log.py or hosts/tmux/"
                    for line, s in _strings(ast.parse(text, rel))
                    if STATUS_LOG_CLOCK in s
                )
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
            f"{module}.{node.name}"
            for module, (_, tree) in trees.items()
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
        _dotted(base, bound) in {"typing.NamedTuple", "enum.Enum", "typing.TypedDict"}
        for base in node.bases
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


def comment_wrap(repo: str) -> list[str]:
    found = []
    for rel, text in _tree_files(repo, ".py"):
        lines = text.splitlines()
        blocks = [*_comment_blocks(text), *_doc_blocks(text)]
        for block in blocks:
            for (line, _, _), (_, following, _) in _prose_pairs(block):
                word = following.split()[0]
                if len(lines[line - 1].rstrip()) + len(word) + 1 <= 100:
                    found.append(f"{rel}:{line}: next word {word!r} fits within 100 columns")
    return sorted(found)


def test_boundaries(repo: str) -> list[str]:
    modules = _source_modules(repo, PROJECTS)
    return sorted(
        f"{rel}:{line}: {reason}"
        for rel, text in _tree_files(repo, ".py")
        for line, reason in _boundary_violations(
            text, rel.split("/")[0].replace("-", "_"), modules, shipped="/src/" in rel
        )
    )
