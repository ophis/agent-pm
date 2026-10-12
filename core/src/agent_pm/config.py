"""The core config's paths, their only home: CONFIG, the core's config.toml; LOCAL, this machine's settings on top.
hosts(config) parses a merged core config's [hosts] (session/hosts/tables.py) and core [delegates] (harness_host);
session_settings(config) gives Session.configure's keyword arguments, config unset: CONFIG with LOCAL. Else
ValueError."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from agent_pm.session.hosts import tables as host_tables
from agent_pm.utils.config_file import read_config
from agent_pm.utils.paths import CORE

CONFIG = os.path.join(CORE, "config.toml")
LOCAL = "~/.agent-pm/core-new.local.toml"


@dataclass(frozen=True)
class Hosts:
    default: str
    tables: dict[str, dict]
    harness: str | None


def hosts(config: Mapping) -> Hosts:
    found = host_tables.parse(config.get("hosts"))
    names = ", ".join(sorted(found.tables))
    delegates = config.get("delegates", {})
    if not isinstance(delegates, Mapping):
        raise ValueError("[delegates]: core takes only harness_host")
    for key in delegates:
        if key != "harness_host":
            raise ValueError(f"[delegates] {key}: core takes only harness_host")
    harness = delegates.get("harness_host")
    if harness is not None and (not isinstance(harness, str) or harness not in found.tables):
        raise ValueError(f"[delegates] harness_host: want one of {names}")
    return Hosts(found.default, found.tables, harness)


def session_settings(config: Mapping | None = None) -> dict:
    if config is None:
        try:
            config = read_config(CONFIG, LOCAL)
        except (OSError, ValueError) as e:
            raise ValueError(f"core config {CONFIG}: {e}") from None
    found = hosts(config)
    return {
        "adapters": config.get("adapters", {}),
        "hosts": found.tables,
        "default_host": found.default,
    }
