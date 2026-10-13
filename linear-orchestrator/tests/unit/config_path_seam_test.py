import json
import os
import subprocess
import tempfile

from orchestrator_test_helpers.case import ORCH_ROOT, PY, child_env
from orchestrator_test_helpers.unit.config.data import IMPORT_ALONE
from orchestrator_test_helpers.unit.config.path_seam_fixtures import PathSeam as PathSeamBase


class PathSeam1(PathSeamBase):
    def test_agent_pm_path_goes_in_front_empty_or_unset_is_the_default(self):
        for home in (self.home, os.path.join(self.home, "other")):
            default = f"/opt/homebrew/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin"
            for env, front in (
                ({"AGENT_PM_PATH": "/fakes/bin:/more"}, "/fakes/bin:/more:"),
                ({}, ""),
                ({"AGENT_PM_PATH": ""}, ""),
            ):
                with self.subTest(home=home, env=env):
                    self.assertEqual(self.path(home, env), front + default)

    def test_importing_reads_no_config_home_or_environment(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home, "AGENT_PM_PATH": "/before/bin"}
            res = subprocess.run(
                [*PY, "-c", IMPORT_ALONE],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=60,
                env=child_env(env, ORCH_ROOT),
            )
            self.assertEqual((res.returncode, res.stderr), (0, ""))
            self.assertEqual(
                json.loads(res.stdout),
                {
                    "read": [],
                    "kept": [],
                    "path": "/later/bin:/opt/homebrew/bin:/later/.local/bin:/usr/local/bin:/usr/bin:/bin",
                },
            )
            self.assertEqual(os.listdir(home), [])
