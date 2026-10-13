"""The delegate's paths, their only home: CONFIG, delegate/config.toml (read through
agent_pm.utils.config_file.read_project("delegate"), ~/.agent-pm/local.toml on top); DELEGATE, the delegate project
dir; IMPORT_ROOTS, every project's src/ beside it (each holding agent_pm/); PYTHONPATH, them joined: a child's.
hosts(config, settings) checks a read delegate config's [delegates] harness_host against the hosts of `settings`,
Session.settings' result for the session config; else ValueError."""

import glob
import os
from collections.abc import Mapping
from dataclasses import dataclass

DELEGATE = os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 4))
IMPORT_ROOTS = tuple(
    sorted(
        os.path.dirname(p)
        for p in glob.glob(os.path.join(os.path.dirname(DELEGATE), "*", "src", "agent_pm"))
    )
)
PYTHONPATH = os.pathsep.join(IMPORT_ROOTS)
CONFIG = os.path.join(DELEGATE, "config.toml")


@dataclass(frozen=True)
class Hosts:
    default: str
    tables: dict[str, dict]
    harness: str | None


def hosts(config: Mapping, settings: Mapping) -> Hosts:
    tables = settings["hosts"]
    names = ", ".join(sorted(tables))
    delegates = config.get("delegates", {})
    if not isinstance(delegates, Mapping):
        raise ValueError("[delegates]: the delegate takes only harness_host")
    for key in delegates:
        if key != "harness_host":
            raise ValueError(f"[delegates] {key}: the delegate takes only harness_host")
    harness = delegates.get("harness_host") or None
    if harness is not None and (not isinstance(harness, str) or harness not in tables):
        raise ValueError(f"[delegates] harness_host: want one of {names}")
    return Hosts(settings["default_host"], dict(tables), harness)
