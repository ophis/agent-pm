from agent_pm.linear_orchestrator.engine import config, prune
from orchestrator_test_helpers.unit.prune.imports_fixtures import Imports as ImportsBase


class Imports1(ImportsBase):
    def test_clones_are_configs(self):
        self.assertIs(prune.CLONES, config.CLONES)
