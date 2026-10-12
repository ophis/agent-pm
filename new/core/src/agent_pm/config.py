"""The core config's paths, their only home: CONFIG, the core's config.toml; LOCAL, this machine's settings on top.
hosts(config) parses a merged core config's [hosts] tables and core [delegates] (harness_host); else ValueError."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from agent_pm.utils import registry
from agent_pm.utils.paths import CORE

CONFIG = os.path.join(CORE, "config.toml")
LOCAL = "~/.agent-pm/core-new.local.toml"
NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


@dataclass(frozen=True)
class Hosts:
    default: str
    tables: dict[str, dict]
    harness: str | None


def hosts(config: Mapping) -> Hosts:
    found = config.get("hosts")
    if not isinstance(found, Mapping):
        raise ValueError("[hosts]: missing")
    tables = {}
    for key, table in found.items():
        if key == "default":
            continue
        if not isinstance(table, Mapping):
            raise ValueError(f"[hosts] {key}: want default or a [hosts.<name>] table")
        if not NAME.match(key):
            raise ValueError(f"[hosts.{key}]: want a name matching [a-z][a-z0-9_]*")
        implementation = table.get("implementation")
        if not isinstance(implementation, str) or not registry.PATH.fullmatch(implementation):
            raise ValueError(f"[hosts.{key}].implementation: want module.path:Class")
        tables[key] = dict(table)
    names = ", ".join(sorted(tables))
    default = found.get("default")
    if not isinstance(default, str) or default not in tables:
        raise ValueError(f"[hosts] default: want one of {names}")
    delegates = config.get("delegates", {})
    if not isinstance(delegates, Mapping):
        raise ValueError("[delegates]: core takes only harness_host")
    for key in delegates:
        if key != "harness_host":
            raise ValueError(f"[delegates] {key}: core takes only harness_host")
    harness = delegates.get("harness_host")
    if harness is not None and (not isinstance(harness, str) or harness not in tables):
        raise ValueError(f"[delegates] harness_host: want one of {names}")
    return Hosts(default, tables, harness)
