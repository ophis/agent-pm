"""How a Docker start tells a role run from a crew worker, the workspace volume names and a
session's state dir. The broker owns the mount-point rules (docs/docker-broker-protocol.md).
"""

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping

from agent_pm.session.engine.types import ConfigError
from agent_pm.utils import docker_home, read_beneath

KIND_LABEL = "agent-pm.kind"  # a workspace volume's labels, set by the broker
WORKSPACE_LABEL = "agent-pm.workspace"
RUN_VOLUME = re.compile(r"agent-pm-run-[A-Za-z0-9_.-]+-[0-9a-f]{8}")
INTENT = os.path.join("run", "container-intent.json")
INTENT_MAX = 1 << 20


def role_run(cwd: str, roots: Iterable[str]) -> Mapping | None:
    """The run intent when `cwd`, a real path, is a role run's agent dir: named `agent`, its
    sibling run/container-intent.json a regular file reached through no link and under one of
    `roots` (real paths); else None, a crew worker. ConfigError: an intent that is no JSON object
    with a delegate_config text.
    """
    workdir = os.path.dirname(cwd)
    root = next((r for r in roots if os.path.commonpath([workdir, r]) == r), None)
    if os.path.basename(cwd) != "agent" or root is None:
        return None
    raw = read_beneath(root, os.path.join(workdir, INTENT), limit=INTENT_MAX + 1)
    if raw is None:
        return None
    try:
        intent = json.loads(raw) if len(raw) <= INTENT_MAX else None
    except (ValueError, RecursionError):
        intent = None
    if not isinstance(intent, dict) or not isinstance(intent.get("delegate_config"), str):
        raise ConfigError(f"Docker cwd {cwd}: {INTENT} is not a run intent")
    return intent


def run_volume(cwd: str) -> str:
    """A role run's workspace volume: its workdir's name and a hash of its agent dir `cwd`."""
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(os.path.dirname(cwd)))[:40] or "run"
    return f"agent-pm-run-{label}-{hashlib.sha256(cwd.encode()).hexdigest()[:8]}"


def crew_volume(name: str) -> str:
    """Session `name`'s workspace volume, kept across its restarts and resumes."""
    return f"agent-pm-crew-{name}"


def state_dir(name: str) -> str:
    """Session `name`'s dir under DOCKER_STATE: its client home, manifests and supervisor files."""
    return os.path.normpath(docker_home(name, os.curdir))
