from orchestrator_test_helpers.case import Case
from orchestrator_test_helpers.unit.config.config_file_fixtures import ConfigFile
from orchestrator_test_helpers.unit.config.data import PIPELINE


class Runnable(ConfigFile, Case):
    def runs(self, text=PIPELINE):
        return self.load(text).roles
