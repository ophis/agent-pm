import os
import subprocess
from unittest import mock

from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.unit.config.shell_fixtures import Shell as ShellBase


class Shell1(ShellBase):
    def test_sh_run(self):
        with (
            mock.patch.object(subprocess, "run", return_value="res") as run,
            mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "KEEP": "1"}),
        ):
            self.assertEqual(config.sh_run(["git", "status"], 5), "res")
            env = {**os.environ, "PATH": config.path()}
        run.assert_called_once_with(
            ["git", "status"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        self.assertEqual(env["KEEP"], "1")
