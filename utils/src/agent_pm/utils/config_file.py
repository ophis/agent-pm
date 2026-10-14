"""Ordered config resources with per-layer local sections; read_project is the repository wrapper.

Tables merge key by key; higher values and whole lists replace lower values. Each layer's local
section overlays its file before the next layer. Sections use the package's final name, with
hyphens replaced by underscores. Only supplied layers are read and validated against their own
and lower files. Unknown top-level keys are rejected; nested tables are validated by their
owners. Flat local keys are rejected. Explicit paths replace files while retaining
the defaults as their validation schema. Missing optional repository files contribute no keys.
"""

import copy
import os
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from importlib.resources import files
from types import MappingProxyType
from typing import Any

from agent_pm.utils.paths import ROOT, STATE

LOCAL = f"{STATE}/config.local.toml"
LEVELS = (("utils", "session"), ("delegate",), ("linear-orchestrator",), ("manager",))
UNCONFIGURED = ("utils", "manager")  # no own file


class ConfigFileError(ValueError):
    pass


def merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


class _Array(tuple):
    """A frozen list; thawed, a list again."""


def freeze(value: Any) -> Any:
    """A read-only deep copy: mappings as MappingProxyType, lists as tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (list, _Array)):
        return _Array(freeze(v) for v in value)
    if isinstance(value, tuple):
        return tuple(freeze(v) for v in value)
    return copy.deepcopy(value)


def thaw(value: Any) -> Any:
    """A fresh mutable deep copy: mappings as dicts, freeze's lists lists again."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, _Array):
        return [thaw(v) for v in value]
    if isinstance(value, tuple):
        return tuple(thaw(v) for v in value)
    return copy.deepcopy(value)


def read_resources(
    resources: Iterable[tuple[str, str]],
    *,
    local: str = LOCAL,
    paths: Mapping[str, str] | None = None,
) -> dict:
    """Read (package name, resource filename) pairs, lower first; paths replace named packages."""
    resources = tuple(resources)
    configs, known = {}, {}
    for package, filename in resources:
        section = package.rsplit(".", 1)[-1].replace("-", "_")
        if section in configs:
            raise ConfigFileError(f"duplicate config layer {section!r}")
        resource = files(package).joinpath(filename)
        with resource.open("rb") as stream:
            default = tomllib.load(stream)
        override = paths.get(package) if paths is not None else None
        configs[section] = load(override) if override is not None else default
        known[section] = (default, configs[section])
    if paths and (extra := paths.keys() - {p for p, _ in resources}):
        raise ConfigFileError(f"config path override has no resource: {min(extra)!r}")
    return _layered(configs, known, local=local)


def read_project(
    name: str, *, local: str = LOCAL, path: str | None = None, root: str = ROOT, own: bool = False
) -> dict:
    """Repository forwarding API; own selects the named layer; path replaces its file."""
    rank = _rank(name)

    def location(project):
        package = project.replace("-", "_")
        packed = os.path.join(root, project, "src", "agent_pm", package, "config.toml")
        return packed if os.path.exists(packed) else os.path.join(root, project, "config.toml")

    own_file = path or location(name)
    if (path or name not in UNCONFIGURED) and (
        os.path.isdir(own_file) or not os.path.exists(own_file)
    ):
        raise FileNotFoundError(f"{own_file}: missing")
    configs, known = {}, {}
    for level in LEVELS[: rank + 1]:
        layer = {}
        for project in level:
            section = project.replace("-", "_")
            default = _load(location(project))
            value = _load(path) if project == name and path else default
            if clash := sorted(set(layer) & set(value)):
                raise ConfigFileError(
                    f"{project}/config.toml: {clash[0]} is also a same-level project's"
                )
            layer.update(value)
            configs[section] = value
            known[section] = (default, value)
    return _layered(configs, known, local=local, own=name.replace("-", "_") if own else None)


def _layered(
    configs: Mapping[str, dict], known: Mapping[str, tuple[dict, dict]], *, local: str, own=None
) -> dict:
    found = _load(os.path.expanduser(local))
    keys = {key for tables in known.values() for table in tables for key in table}
    if flat := sorted(
        key for key, value in found.items() if not isinstance(value, dict) or key in keys
    ):
        key = flat[0]
        path, _ = next(_leaves({key: found[key]}, ()))
        schemas = {p: merge(*tables) for p, tables in known.items() if p != "utils" and any(tables)}
        targets = _flat_sections(path, schemas) or list(schemas)
        sections = ", ".join(f"[{p}]" for p in targets)
        hint = (
            sections
            if len(targets) == 1
            else f"one of {sections}"
            if targets
            else "a supplied layer section"
        )
        raise ConfigFileError(
            f"{local}: unknown key {'.'.join(path)!r}: flat local configuration; move it under {hint}"
        )
    schema, out = {}, {}
    for section, config in configs.items():
        schema = merge(schema, merge(*known[section]))
        overlay = found.get(section, {})
        if not isinstance(overlay, dict):
            raise ConfigFileError(f"{local}: [{section}]: want a table")
        checked = {}
        for keys, value in _leaves(overlay, ()):
            if keys[0] not in schema:
                raise ConfigFileError(f"{local}: unknown key {keys[0]!r} in [{section}]")
            if value == {}:
                continue
            table = checked
            for key in keys[:-1]:
                table = table.setdefault(key, {})
            table[keys[-1]] = value
        if own is None:
            out = merge(out, merge(config, checked))
        elif section == own:
            out = merge(config, checked)
    return out


def _flat_sections(keys: tuple, schemas: Mapping[str, dict]) -> list[str]:
    having = [p for p, table in schemas.items() if keys[0] in table]
    exact = [p for p in having if _has(schemas[p], keys)]
    shaped = [
        p
        for p in having
        if any(len(k) == len(keys) and k[-1] == keys[-1] for k, _ in _leaves(schemas[p], ()))
    ]
    suffix = [p for p in having if any(_has(schemas[p], keys[i:]) for i in range(1, len(keys)))]
    return exact or shaped or suffix or having


def unset(cfg: Mapping, keys: tuple[str, ...]) -> list[str]:
    """The dotted `keys` (a `*` segment: each subtable) missing from `cfg` or still "" there:
    required, unset.
    """
    return [k for key in keys for k, v in _values(cfg, key.split("."), []) if v in (None, "")]


def _rank(name: str) -> int:
    for i, level in enumerate(LEVELS):
        if name in level:
            return i
    raise ConfigFileError(
        f"project {name!r}: want one of {', '.join(p for level in LEVELS for p in level)}"
    )


def _load(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


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
