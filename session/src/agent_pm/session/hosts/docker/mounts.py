"""The cwd rule a Docker start checks first, the workspace volume names and a session's state
dir. The broker owns the mount-point rules (docs/docker-broker-protocol.md).
"""

import os
import re

from agent_pm.session.engine.types import ConfigError
from agent_pm.utils import docker_home

ROLE_RUNS = ".agent-pm"
RUN_VOLUME = re.compile(r"agent-pm-run-[A-Za-z0-9_.-]+-[0-9a-f]{8}")


def check_cwd(cwd: str, home: str) -> None:
    """ConfigError for a role run's cwd (a real path inside ~/.agent-pm): not run on Docker yet."""
    runs = os.path.join(home, ROLE_RUNS)
    if os.path.commonpath([cwd, runs]) == runs:
        raise ConfigError(f"Docker cwd {cwd}: role runs are not supported on the Docker Host yet")


def crew_volume(name: str) -> str:
    """Session `name`'s workspace volume, kept across its restarts and resumes."""
    return f"agent-pm-crew-{name}"


def state_dir(name: str) -> str:
    """Session `name`'s dir under DOCKER_STATE: its client home, manifests and supervisor files."""
    return os.path.normpath(docker_home(name, os.curdir))
