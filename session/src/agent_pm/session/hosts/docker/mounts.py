"""The workspace volume name and a session's state dir. The broker owns the mount-point rules
(protocol/docker-broker/protocol.schema.json).
"""

import os

from agent_pm.utils import docker_home

KIND_LABEL = "agent-pm.kind"  # a workspace volume's labels, set by the broker
WORKSPACE_LABEL = "agent-pm.workspace"


def workspace_volume(name: str) -> str:
    """Session `name`'s workspace volume, kept across its restarts and resumes."""
    return f"agent-pm-ws-{name}"


def state_dir(name: str) -> str:
    """Session `name`'s dir under DOCKER_STATE: its client home, manifests and supervisor files."""
    return os.path.normpath(docker_home(name, os.curdir))
