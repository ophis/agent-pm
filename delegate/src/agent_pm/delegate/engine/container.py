"""A run's container intent, <workdir>/run/container-intent.json, written before every session start
whatever the Host; the Docker Host builds a role run's container from it (docs/docker-host.md).
"""

import itertools
import json
import os
from collections.abc import Sequence

from agent_pm.delegate.engine.types import ConfigError, Layout, Plan
from agent_pm.utils import GUARD, GitError, Invalid, Repo, Runner, origin, run_git, save, sh

INTENT = "container-intent.json"


def reference_repo(text: str, *, run: Runner | None = None) -> str | None:
    """The github.com owner/name of the one `Repo:` line in the input's leading `Reference:` block (a
    local clone's from its origin, read with GUARD); None without one, or naming none on github.com.
    """
    block = list(itertools.takewhile(str.strip, text.lstrip().splitlines()))
    if not block or not block[0].startswith("Reference:"):
        return None
    found = sorted(
        {line.removeprefix("Repo:").strip() for line in block if line.startswith("Repo:")}
    )
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


def intent(plan: Plan, users: Sequence[str]) -> dict:
    """`users`: the Delegate config's; any `[bot]` login is left out of the container's."""
    if not isinstance(users, list | tuple) or not all(isinstance(u, str) for u in users):
        raise ConfigError("users must be a list of GitHub logins")
    output = plan.run.output
    humans = [user for user in users if not user.endswith("[bot]")]
    return {
        "role": plan.run.role,
        "repo": plan.repo,
        "output_repo": (output.get("repo") or None) if output["type"] == "github" else None,
        "delegate_config": f"[delegate]\nusers = {json.dumps(humans)}\ntrusted_dirs = []\n",
    }


def write_intent(layout: Layout, plan: Plan, users: Sequence[str]) -> None:
    """Owner-only, as save makes it."""
    save(os.path.join(layout.run, INTENT), json.dumps(intent(plan, users)))
