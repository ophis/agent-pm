"""The delegate's paths, their only home: CONFIG (read by read_project("delegate")), DELEGATE,
IMPORT_ROOTS and PYTHONPATH (them joined: a child's); its keys' parsers, raising ValueError:
harness_host (None unset, else one of `hosts`), policy, trusted_dirs.
"""

import os
from collections.abc import Collection, Mapping

from agent_pm.delegate.engine.settings.types import SettingsError, SettingsTypeError
from agent_pm.utils import import_roots, read_project, real_dirs

DELEGATE = os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 4))
IMPORT_ROOTS = import_roots()
PYTHONPATH = os.pathsep.join(IMPORT_ROOTS)
CONFIG = os.path.join(DELEGATE, "config.toml")


def harness_host(config: Mapping, hosts: Collection[str]) -> str | None:
    delegates = config.get("delegates", {})
    if not isinstance(delegates, Mapping):
        raise SettingsTypeError.delegates()
    for key in delegates:
        if key != "harness_host":
            raise SettingsError.harness_key(key)
    harness = delegates.get("harness_host") or None
    if harness is not None and (not isinstance(harness, str) or harness not in hosts):
        raise SettingsError.harness_host(hosts)
    return harness


def policy(runtime: str, cfg: Mapping | None = None) -> tuple[list, dict]:
    """[policy.<runtime>]'s flags and env, unchecked; `cfg` None: read."""
    tables = (read_project("delegate") if cfg is None else cfg).get("policy", {})
    if not isinstance(tables, Mapping):
        raise SettingsTypeError.policy()
    value = tables.get(runtime, {})
    if not isinstance(value, Mapping):
        raise SettingsTypeError.runtime(runtime)
    if set(value) - {"flags", "env"}:
        raise SettingsError.policy_keys(runtime)
    flags, env = value.get("flags", []), value.get("env", {})
    if not isinstance(flags, list):
        raise SettingsTypeError.flags(runtime)
    return flags, env


def trusted_dirs(cfg: Mapping) -> frozenset[str]:
    return real_dirs(cfg.get("trusted_dirs", []), "trusted_dirs")
