"""The delegate's paths, their only home: CONFIG (read by read_project("delegate")), DELEGATE, IMPORT_ROOTS and
PYTHONPATH (them joined: a child's). harness_host: [delegates] harness_host, None unset, else one of `hosts`."""

import os
from collections.abc import Collection, Mapping

from agent_pm.utils.paths import import_roots

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
