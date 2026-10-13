import os

from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.config.data import CORE_LOCAL, CORE_TABLES, CORE_TOP, write


class RealConfig(Case):
    def setUp(self):
        write(os.path.join(self.agent_pm, "config.local.toml"), CORE_LOCAL)
        self.local = os.path.join(self.agent_pm, "config.local.toml")

    def load(self, local=None):
        if local is not None:
            write(self.local, CORE_TOP + local + "\n" + CORE_TABLES)
        return config.load()
