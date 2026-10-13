import os
import subprocess
from types import SimpleNamespace

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine import config, target
from agent_pm.linear_orchestrator.linear import issues
from agent_pm.utils import GUARD

PROJ = "121166b1-191a-4461-bec4-42f1c2dc0ddd"

NO_LINE = target.Invalid("no Repo: line in the description or its ## Instructions")

LOCAL = ("git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C")


def roles():
    """Each role's tasks: the committed core config's index (no local file)."""
    return {
        r: SimpleNamespace(
            tasks=tuple(delegate.task_index(r, config=delegate.CONFIG, local=os.devnull))
        )
        for r in config.TASKS
    }


def issue(description, title="ENG: Session Registry", ident="TASK-26", project=None):
    return issues.Issue(
        id="uuid-26",
        identifier=ident,
        url=f"https://linear.app/t/issue/{ident}",
        title=title,
        description=description,
        created_at="2026-09-01T00:00:00.000Z",
        project_id=project,
        notes=(),
        links=(),
        linked=(),
        handoff=issues.parse_handoff(description),
    )


def git(*args):
    subprocess.run(
        ["git", *GUARD, *args], check=True, capture_output=True, stdin=subprocess.DEVNULL
    )
