from orchestrator_test_helpers.unit.router.data import CONFIG, HUMANS, TASK_LABELS
from orchestrator_test_helpers.unit.router.tick_base_fixtures import TickBase


class TickTaskLabels(TickBase):
    def setUp(self):
        super().setUp()
        self.write_config(HUMANS + CONFIG + TASK_LABELS)
