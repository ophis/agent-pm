"""The broker's runtime mount rules a Docker start checks first, and a session's state dir."""

import os

from agent_pm.session.engine.types import ConfigError
from agent_pm.utils import docker_home

# The protocol's deny list below the home, minus each client's own dir: Session code may not name
# a client, so the broker's mount_rejected refuses those, still before any container.
DENIED = (".agent-pm", ".ssh", ".config", ".docker", "Library", ".aws", ".gnupg", ".kube", ".npmrc")
ROLE_RUNS = ".agent-pm"


def _within(path: str, root: str) -> bool:
    return os.path.commonpath([path, root]) == root


def check_cwd(cwd: str, roots, home: str) -> None:
    """ConfigError naming `cwd` (a real path) and the rule it breaks; roots are real paths."""
    why = None
    if not roots:
        why = (
            "[hosts.docker] mount_roots is empty; set it in the local config (docs/docker-host.md)"
        )
    elif bad := next((r for r in roots if _within(home, r)), None):
        why = f"mount root {bad} is the home or one of its ancestors"
    elif not any(_within(cwd, r) for r in roots):
        why = "it is outside every mount root"
    elif _within(home, cwd):
        why = "it is the home or one of its ancestors"
    else:
        for name in DENIED:
            denied = os.path.join(home, name)
            if _within(cwd, denied) or _within(denied, cwd):
                why = f"it overlaps ~/{name}"
                if name == ROLE_RUNS:
                    why += "; role runs are not supported on the Docker Host yet"
                break
    if why:
        raise ConfigError(f"Docker cwd {cwd}: {why}")


def state_dir(name: str) -> str:
    """Session `name`'s dir under DOCKER_STATE: its client home, manifests and supervisor files."""
    return os.path.normpath(docker_home(name, os.curdir))
