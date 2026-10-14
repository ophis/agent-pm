"""Human resume commands preserve exec environment names through the shell."""

import json
import os
import subprocess
import sys

from agent_pm.utils import resume_command

from utils.tests.case import Case
from utils.tests.sandbox import clean_env


class ResumeEnvironment(Case):
    def test_exec_names_and_shell_metacharacters_reach_the_child_unchanged(self):
        values = {"A-B": "a b", "X;Y": "$(false); 'x'", "-x": "", "A B": "v"}
        command = resume_command(
            os.getcwd(),
            values,
            (
                sys.executable,
                "-I",
                "-c",
                "import json, os, sys; print(json.dumps({k: os.environ[k] for k in sys.argv[1:]}))",
                *values,
            ),
        )
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            env=clean_env(),
        )
        self.assertEqual(json.loads(result.stdout), values)
