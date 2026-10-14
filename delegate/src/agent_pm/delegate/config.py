"""Packaged Delegate configuration and its independent setting parsers."""

import os
from collections.abc import Collection, Mapping

from agent_pm import utils
from agent_pm.utils import read_resources, real_dirs

CONFIG: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml")


def read_config(*, local: str = utils.LOCAL, path: str | None = None) -> dict:
    return read_resources(
        (("agent_pm.session", "config.toml"), ("agent_pm.delegate", "config.toml")),
        local=local,
        paths=None if path is None else {"agent_pm.delegate": path},
    )


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
    tables = (read_config() if cfg is None else cfg).get("policy", {})
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
