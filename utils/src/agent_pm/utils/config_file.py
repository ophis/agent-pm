"""TOML config loading and table merging, and the projects' config files: each project's <root>/<name>/config.toml,
merged in LEVELS order (a higher level's key wins), with LOCAL, this machine's one local file, on top of all.

Merge rules (merge): tables merge key by key, the higher layer's value wins, any other value (a list too) replaces
the lower one's whole; a layer cannot drop a key a lower one sets.

Ownership of a local key: a top-level key in one project's config.toml is that project's. A top-level table in
several (roles) is resolved by the full dotted key: the projects whose config.toml has that key, else one with the
same table, depth and last key (a key of a role table it does not list, e.g. roles.<new role>.account), else every
project with the table (e.g. roles.<role>.cwd, a run key). read_project(name) takes the local keys owned at or below
name's level; a local top-level key no project has is a ValueError."""

import os
import tomllib
from collections.abc import Iterator, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 4))
LOCAL = "~/.agent-pm/local.toml"
LEVELS = (("utils", "session"), ("delegate",), ("linear-orchestrator",), ("manager",))


def merge(base: dict, over: dict) -> dict:
    """`over` on `base`: tables merge key by key; any other value replaces base's."""
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read_config(path: str, local: str) -> dict:
    """`path`, with `local` (~ expanded) merged on top when it exists."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    local = os.path.expanduser(local)
    if os.path.isfile(local):
        with open(local, "rb") as f:
            cfg = merge(cfg, tomllib.load(f))
    return cfg


def read_project(
    name: str, *, local: str = LOCAL, path: str | None = None, root: str = ROOT
) -> dict:
    """Every project config.toml at or below `name`'s level, in LEVELS order, then `local`'s keys owned at or below
    it. `path` stands in for `name`'s own config.toml. A missing config.toml or local file is empty."""
    rank = _rank(name)
    configs = _configs(root, name, path)
    out = {}
    for level in LEVELS[: rank + 1]:
        layer = {}
        for project in level:
            if clash := sorted(set(layer) & set(configs[project])):
                raise ValueError(
                    f"{project}/config.toml: {clash[0]} is also a same-level project's"
                )
            layer.update(configs[project])
        out = merge(out, layer)
    found = _load(os.path.expanduser(local))
    if unknown := sorted(k for k in found if not any(k in c for c in configs.values())):
        raise ValueError(f"{local}: unknown key {unknown[0]!r}: no project's config.toml has it")
    return merge(out, _owned(found, configs, rank))


def unset(cfg: Mapping, keys: tuple[str, ...]) -> list[str]:
    """The dotted `keys` (a `*` segment: each subtable) missing from `cfg` or still "" there: required, unset."""
    return [k for key in keys for k, v in _values(cfg, key.split("."), []) if v in (None, "")]


def _rank(name: str) -> int:
    for i, level in enumerate(LEVELS):
        if name in level:
            return i
    raise ValueError(
        f"project {name!r}: want one of {', '.join(p for level in LEVELS for p in level)}"
    )


def _configs(root: str, name: str, path: str | None) -> dict[str, dict]:
    return {
        p: _load(path if p == name and path else os.path.join(root, p, "config.toml"))
        for level in LEVELS
        for p in level
    }


def _load(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _owned(found: dict, configs: dict[str, dict], rank: int) -> dict:
    out = {}
    for keys, value in _leaves(found, ()):
        if min(_rank(p) for p in _owners(keys, configs)) <= rank:
            table = out
            for k in keys[:-1]:
                table = table.setdefault(k, {})
            table[keys[-1]] = value
    return out


def _owners(keys: tuple, configs: dict[str, dict]) -> list[str]:
    having = [p for p, c in configs.items() if keys[0] in c]
    if len(having) == 1:
        return having
    exact = [p for p in having if _has(configs[p], keys)]
    shaped = [
        p
        for p in having
        if isinstance(top := configs[p][keys[0]], Mapping)
        and any(len(k) == len(keys) and k[-1] == keys[-1] for k, _ in _leaves(top, keys[:1]))
    ]
    return exact or shaped or having


def _has(table, keys) -> bool:
    for k in keys:
        if not isinstance(table, Mapping) or k not in table:
            return False
        table = table[k]
    return True


def _leaves(table: Mapping, at: tuple) -> Iterator[tuple[tuple, object]]:
    if not table and at:
        yield at, {}
    for k, v in table.items():
        if isinstance(v, Mapping):
            yield from _leaves(v, (*at, k))
        else:
            yield (*at, k), v


def _values(cfg, keys: list[str], at: list[str]) -> Iterator[tuple[str, object]]:
    if not keys:
        yield ".".join(at), cfg
        return
    head, rest = keys[0], keys[1:]
    if head == "*":
        for k, v in cfg.items() if isinstance(cfg, Mapping) else ():
            yield from _values(v, rest, [*at, k])
    elif isinstance(cfg, Mapping) and head in cfg:
        yield from _values(cfg[head], rest, [*at, head])
    elif "*" not in keys:
        yield ".".join([*at, *keys]), None
