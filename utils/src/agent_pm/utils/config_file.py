"""TOML config loading and table merging, and the projects' config files: each project's <root>/<name>/config.toml,
merged in LEVELS order (a higher level's key wins), with LOCAL, this machine's one local file, on top of all.

Merge rules (merge): tables merge key by key, the higher layer's value wins, any other value (a list too) replaces
the lower one's whole; a layer cannot drop a key a lower one sets.

Ownership of a local key: a top-level key in one project's config.toml is that project's. A top-level table in
several (roles) is resolved by the full dotted key, among the projects with that table: those whose config.toml has
the key, else the same table, depth and last key (roles.<new role>.account), else a suffix of the key
(roles.<role>.cwd: cwd), else all of them; a local top-level key no project has is a ValueError."""

import os
import tomllib
from collections.abc import Iterator, Mapping

from agent_pm.utils.paths import ROOT

LOCAL = "~/.agent-pm/config.local.toml"
LEVELS = (("utils", "session"), ("delegate",), ("linear-orchestrator",), ("manager",))
UNCONFIGURED = ("utils", "manager")  # no own file


def merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read_config(path: str, local: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    local = os.path.expanduser(local)
    if os.path.isfile(local):
        with open(local, "rb") as f:
            cfg = merge(cfg, tomllib.load(f))
    return cfg


def read_project(
    name: str, *, local: str = LOCAL, path: str | None = None, root: str = ROOT, own: bool = False
) -> dict:
    """Each project config.toml at or below `name`'s level, then `local`'s keys owned there; `own`: `name`'s file and
    the local keys it owns. `path` stands in for `name`'s config.toml. Missing: `path` or an own file is an OSError."""
    rank = _rank(name)
    own_file = path or os.path.join(root, name, "config.toml")
    if (path or name not in UNCONFIGURED) and not os.path.exists(own_file):
        raise FileNotFoundError(f"{own_file}: missing")
    configs = _configs(root, name, path)
    known = (
        {**configs, name: merge(_load(os.path.join(root, name, "config.toml")), configs[name])}
        if path
        else configs
    )
    out = {}
    for level in () if own else LEVELS[: rank + 1]:
        layer = {}
        for project in level:
            if clash := sorted(set(layer) & set(configs[project])):
                raise ValueError(
                    f"{project}/config.toml: {clash[0]} is also a same-level project's"
                )
            layer.update(configs[project])
        out = merge(out, layer)
    found = _load(os.path.expanduser(local))
    if unknown := sorted(k for k in found if not any(k in c for c in known.values())):
        raise ValueError(f"{local}: unknown key {unknown[0]!r}: no project's config.toml has it")
    if own:
        return merge(configs[name], _owned(found, known, lambda owners: name in owners))
    return merge(out, _owned(found, known, lambda owners: min(map(_rank, owners)) <= rank))


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


def _owned(found: dict, configs: dict[str, dict], taken) -> dict:
    out = {}
    for keys, value in _leaves(found, ()):
        if value == {} and not any(_has(c, keys) for c in configs.values()):
            continue
        if taken(_owners(keys, configs)):
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
    suffix = [p for p in having if any(_has(configs[p], keys[i:]) for i in range(1, len(keys)))]
    return exact or shaped or suffix or having


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
