"""delegate/skills/act-as/SKILL.md as Claude Code runs it: its pre-approved rule is step 1's
command, which lists the team with no PYTHON* variable inherited; step 3 runs the same Python, and
step 4 puts its PYTHONPATH before each command of the prompt that runs an agent_pm module or team
tool.
"""

import re

from delegate.tests.case import Case
from delegate.tests.skills.act_as import command, head, prefix, text

RULE = re.compile(r"^allowed-tools:\n  - Bash\((.+)\)$", re.M)
ROLES = ["researcher", "pm", "engineer", "dummy-tester"]


class ActAs(Case):
    def test_the_rule_is_step_1s_command_and_step_3_runs_its_python(self):
        self.assertEqual(RULE.search(text())[1], command(1))
        self.assertEqual(head(), command(1).removesuffix(" --list"))

    def test_step_4s_prefix_is_step_1s(self):
        self.assertEqual(prefix(), command(1).partition(" python3 ")[0])

    def test_step_3_requests_instruction_without_runtime_options(self):
        self.assertIn("agent_pm.delegate.cli prompt --role", command(3))
        for option in ("--runtime", "--mode", "--detach", "--sid", "--resume"):
            self.assertNotIn(option, command(3))

