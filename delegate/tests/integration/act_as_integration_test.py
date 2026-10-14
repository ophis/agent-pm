"""The approved skill command in an isolated Python environment."""

import os
import subprocess

from agent_pm.delegate import cli, composer

from delegate.tests.case import Case
from delegate.tests.skills.act_as import argv, command
from utils.tests.sandbox import clean_env, python_path

ROLES = ["researcher", "pm", "engineer", "dummy-tester"]


class ActAs(Case):
    def test_step_1s_command_lists_the_team(self):
        env = clean_env()
        env["PATH"] = python_path(os.path.join(self.home, "bin"))
        res = subprocess.run(
            argv(command(1)),
            cwd=self.home,
            env={**env, "HOME": self.home},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        want = cli.listing(composer.team(composer.TEAM, composer.team_dirs()))
        self.assertEqual((res.returncode, res.stdout), (0, want), res.stderr)
        self.assertEqual([r.split()[0] for r in want.splitlines() if r[:1] != " "], ROLES)
