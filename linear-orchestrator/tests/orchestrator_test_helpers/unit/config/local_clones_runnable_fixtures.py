import json

from orchestrator_test_helpers.unit.config.clones_fixtures import Clones
from orchestrator_test_helpers.unit.config.data import PIPELINE


class LocalClonesRunnable(Clones):
    def runs(self, key, path):
        return self.load(PIPELINE + f'[local_clones]\n"{key}" = {json.dumps(path)}\n').roles
