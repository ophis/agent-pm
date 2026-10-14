"""Configuration loading, level and key checks."""

import ast
import os
import re
import tomllib

from tests.guard_checks.common import _bindings, _dotted, _files
from tests.guards import (
    CONFIG_LEVELS,
    CONFIG_PY,
    ENTRIES,
    LOAD_HELPERS,
    ONCE,
    ORCH_PROJECT,
    PKG,
    PROJECTS,
)

LOCAL_EXAMPLE = re.compile(r"\s*# local: ?(.*)")


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
    for rel in (
        path
        for project, package in PROJECTS.items()
        for path in (
            f"{project}/config.toml",
            f"{project}/src/{PKG}/{package}/config.toml",
        )
    ):
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
