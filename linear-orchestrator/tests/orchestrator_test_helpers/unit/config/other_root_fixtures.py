import os

from agent_pm.linear_orchestrator.engine import config
from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.config.config_file_fixtures import ConfigFile
from orchestrator_test_helpers.unit.config.data import CORE_TOML, write


class OtherRoot(ConfigFile, Case):
    def setUp(self):
        super().setUp()
        os.remove(os.path.join(self.agent_pm, "config.local.toml"))
        self.root = os.path.join(self.dir, "my root")
        self.dir = os.path.join(self.root, "linear-orchestrator")
        os.makedirs(self.dir)
        os.makedirs(os.path.join(self.root, "delegate"))
        self.core_path = os.path.join(self.root, "delegate", "config.toml")
        self.core(CORE_TOML)

    def core(self, text):
        write(self.core_path, text)

    def load(self, text):
        path = os.path.join(self.dir, "config.toml")
        write(path, text)
        return config.load(self.root, path=path, core=self.core_path)
