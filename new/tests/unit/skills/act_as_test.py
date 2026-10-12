"""new/core/skills/act-as/SKILL.md as Claude Code runs it: its pre-approved rule is step 1's command, which lists the
team with no PYTHON* variable inherited; step 3 runs the same Python, and step 4 puts its PYTHONPATH before each
command of the prompt that runs an agent_pm module."""

import os
import re
import shlex
import subprocess

from agent_pm.delegate import cli, composer

from new.tests.case import CORE, Case

SKILL_DIR = os.path.join(CORE, "skills", "act-as")
RULE = re.compile(r"^allowed-tools:\n  - Bash\((.+)\)$", re.M)
ROLES = ["researcher", "pm", "engineer", "dummy-tester"]


def text() -> str:
    with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as f:
        return f.read()


def command(step: int) -> str:
    line = re.search(rf"^{step}\. .*$", text(), re.M)[0]
    return re.search(r"`(env [^`]+)`", line)[1]


def head() -> str:
    """Step 3's command before its options."""
    return command(3).partition(" --runtime ")[0]


def prefix() -> str:
    """What step 4 puts before each command of the prompt that runs an agent_pm module."""
    return command(4)


def argv(command: str) -> list[str]:
    """command as Bash splits it once Claude Code substitutes ${CLAUDE_SKILL_DIR}."""
    return shlex.split(command.replace("${CLAUDE_SKILL_DIR}", SKILL_DIR))


class ActAs(Case):
    def test_the_rule_is_step_1s_command_and_step_3_runs_its_python(self):
        self.assertEqual(RULE.search(text())[1], command(1))
        self.assertEqual(head(), command(1).removesuffix(" --list"))

    def test_step_4s_prefix_is_step_1s(self):
        self.assertEqual(prefix(), command(1).partition(" python3 ")[0])

    def test_step_1s_command_lists_the_team(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
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
