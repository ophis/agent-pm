import os
from unittest import mock

from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.case import Case


class PathSeam(Case):
    def path(self, home, env):
        """config.path() with HOME `home` and AGENT_PM_PATH as in `env` (unset when absent)."""
        rest = {k: v for k, v in os.environ.items() if k != "AGENT_PM_PATH"}
        with mock.patch.dict(os.environ, {**rest, "HOME": home, **env}, clear=True):
            return config.path()
