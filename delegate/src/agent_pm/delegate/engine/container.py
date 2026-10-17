"""What the Delegate changes for a run whose agent is on the Docker Host (docs/docker-host.md): the
test for that Host, the input's Reference repo and the container's users.
"""

import itertools
import json
import os
from collections.abc import Mapping

from agent_pm.delegate.engine.types import ConfigError
from agent_pm.session import ConfigError as SessionConfigError
from agent_pm.session import Host
from agent_pm.utils import GUARD, GitError, Invalid, Repo, Runner, origin, run_git, sh

DOCKER_HOST = "agent_pm.session.hosts.docker.host:DockerHost"


def on_docker(settings: Mapping, host: str | None) -> bool:
    """Whether `host` (None: the default) is the Docker Host in the config's [hosts]."""
    try:
        hosts = Host.parse_hosts(settings.get("hosts"))
    except SessionConfigError as error:
        raise ConfigError(str(error)) from None
    return hosts.tables.get(host or hosts.default, {}).get("implementation") == DOCKER_HOST


def _reference(text: str) -> tuple[list[str], int]:
    """(text's lines, kept ends, after leading blank ones; how many its Reference block holds)."""
    lines = text.lstrip().splitlines(keepends=True)
    if not lines or not lines[0].startswith("Reference:"):
        return lines, 0
    return lines, len(list(itertools.takewhile(str.strip, lines)))


def repo_lines(text: str) -> list[str]:
    """The distinct values of the `Repo:` lines in the input's leading `Reference:` block."""
    lines, n = _reference(text)
    return sorted(
        {line.removeprefix("Repo:").strip() for line in lines[:n] if line.startswith("Repo:")}
    )


def with_repo(text: str, repo: str) -> str:
    """`text`, each `Repo:` line of its Reference block naming `repo`."""
    lines, n = _reference(text)
    for i in range(n):
        if lines[i].startswith("Repo:"):
            lines[i] = f"Repo: {repo}" + lines[i][len(lines[i].rstrip("\r\n")) :]
    return "".join(lines)


def reference_repo(text: str, *, run: Runner | None = None) -> str | None:
    """The github.com owner/name of the one `Repo:` line in the input's leading `Reference:` block (a
    local clone's from its origin, read with GUARD); None without one, or naming none on github.com.
    """
    found = repo_lines(text)
    if len(found) > 1:
        raise ConfigError(f"the Reference block names more than one Repo: {', '.join(found)}")
    return _github(found[0], run or sh) if found else None


def _github(spec: str, run: Runner) -> str | None:
    if spec.startswith(("/", "~")):
        clone = os.path.realpath(os.path.expanduser(spec))
        try:
            spec = origin(run_git(clone, "remote", "get-url", "origin", run=run, pre=GUARD)) or ""
        except GitError:
            return None
    try:
        repo = Repo.parse(spec)
    except Invalid:
        return None
    return f"{repo.owner}/{repo.name}" if repo.host == "github.com" else None


def users_env(users) -> dict[str, str]:
    """AGENT_PM_USERS for a Docker run's container: the Delegate config's users, no `[bot]` login."""
    if not isinstance(users, list | tuple) or not all(isinstance(u, str) for u in users):
        raise ConfigError("users must be a list of GitHub logins")
    return {"AGENT_PM_USERS": json.dumps([u for u in users if not u.endswith("[bot]")])}
