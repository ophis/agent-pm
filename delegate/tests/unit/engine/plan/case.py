"""Shared fixtures for plan behavior."""

import os
import re

from agent_pm.delegate import composer
from agent_pm.utils import temp_dirs, under

from delegate.tests.case import REPO
from delegate.tests.unit.composer.teams import POET, VERSE

TASKS = os.path.join(composer.TEAM, "tasks")


TASKS_PARAM = re.compile(r"\n- `<tasks>`: `([^`]+)`\n")


TEAM_FILES = {"roles/poet.md": POET, "tasks/verse.md": VERSE}


TEAMS = os.path.join(REPO, "work", "new-team-dirs-tests")


IN_TEMP = under(os.path.realpath(TEAMS), temp_dirs()) is not None


def notice(cwd):
    return f"delegate: cwd {cwd} is not in trusted_dirs: its project settings are off\n"


def write_team(top, files=TEAM_FILES):
    for rel, text in files.items():
        path = os.path.join(top, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.chmod(os.path.dirname(path), 0o755)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o644)
