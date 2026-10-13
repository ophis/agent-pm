"""The delegate's paths, their only home: CONFIG (read by read_project("delegate")), DELEGATE,
IMPORT_ROOTS and PYTHONPATH (them joined: a child's); its keys' parsers, raising ValueError:
harness_host (None unset, else one of `hosts`), policy, trusted_dirs.
"""

import os
from collections.abc import Collection, Mapping

from agent_pm.utils import import_roots, read_project, real_dirs

DELEGATE = os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 4))
IMPORT_ROOTS = import_roots()
PYTHONPATH = os.pathsep.join(IMPORT_ROOTS)
CONFIG = os.path.join(DELEGATE, "config.toml")


def harness_host(config: Mapping, hosts: Collection[str]) -> str | None:
    delegates = config.get("delegates", {})
    if not isinstance(delegates, Mapping):
        raise ValueError("[delegates]: the delegate takes only harness_host")
    for key in delegates:
        if key != "harness_host":
            raise ValueError(f"[delegates] {key}: the delegate takes only harness_host")
    harness = delegates.get("harness_host") or None
    if harness is not None and (not isinstance(harness, str) or harness not in hosts):
        raise ValueError(f"[delegates] harness_host: want one of {', '.join(sorted(hosts))}")
    return harness


def policy(runtime: str, cfg: Mapping | None = None) -> tuple[list, dict]:
    """[policy.<runtime>]'s flags and env, unchecked; `cfg` None: read."""
    tables = (read_project("delegate") if cfg is None else cfg).get("policy", {})
    if not isinstance(tables, Mapping):
        raise ValueError("policy must be a table")
    value = tables.get(runtime, {})
    if not isinstance(value, Mapping) or set(value) - {"flags", "env"}:
        raise ValueError(f"policy.{runtime} must contain only flags/env")
    flags, env = value.get("flags", []), value.get("env", {})
    if not isinstance(flags, list):
        raise ValueError(f"policy.{runtime}.flags must be a list of strings without NUL")
    return flags, env


def trusted_dirs(cfg: Mapping) -> frozenset[str]:
    return real_dirs(cfg.get("trusted_dirs", []), "trusted_dirs")
