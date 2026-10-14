"""Layer command checks: who runs which agent-pm command, and how Python runs."""

import ast
import os
import re

from tests.guard_checks.common import _bindings, _dotted, _files, _strings, _tree_files
from tests.guard_data import COMMAND_DOCS, COMMAND_FILES, COMMAND_SRC
from tests.guard_data.session import SESSION_COMMAND, SESSION_SRC
from tests.guards import PKG

COMMAND = "agent-pm-"


SPAWNS = re.compile(r"subprocess\.\w+|os\.(?:exec|spawn)\w*")


PYTHON = re.compile(r"\bpython(?:3(?:\.\d+)?)?\b(?:[ \t]+[^\s`'\"]+){0,3}")


PYTHON_OK = re.compile(
    f"\\S+[ \\t]+-P[ \\t]+(?:-m[ \\t]+{PKG}\\.\\w|(?:<team>|\\{{\\{{team\\}}\\}})/tools/\\w+\\.py(?=[ \\t]|\\Z))"
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
