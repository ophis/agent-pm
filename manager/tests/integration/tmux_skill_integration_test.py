"""manager/skills/{tmux,manage}/SKILL.md's layer command examples against the real bin/ commands:
each subcommand's --help exits 0 and lists every flag the example writes.
"""

import os
import re
import subprocess

from manager.tests.case import Case, child_env
from manager.tests.skills.tmux import BIN, body, examples, flags, subcommand

# The router gains --tui with its pipeline Record (TASK-332); drop this entry then.
AHEAD = {("agent-pm-linear", "router", "--tui")}


class Examples(Case):
    def help(self, command: str, sub: str | None) -> str:
        result = subprocess.run(
            (os.path.join(BIN, command), *([sub] if sub else []), "--help"),
            cwd=self.home,
            env=child_env(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"{command} {sub} --help: {result.stderr}")
        return result.stdout

    def test_every_example_names_a_subcommand_and_flags_its_command_takes(self):
        helps = {}
        for command in (*examples(body("tmux")), *examples(body("manage"))):
            key = command[0], subcommand(command)
            if key not in helps:
                helps[key] = self.help(*key)
            for flag in flags(command):
                if (*key, flag) not in AHEAD:
                    self.assertRegex(helps[key], rf"(?<![\w-]){re.escape(flag)}(?![\w-])", command)
