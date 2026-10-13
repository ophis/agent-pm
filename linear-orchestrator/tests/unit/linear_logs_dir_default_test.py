from agent_pm.linear_orchestrator.linear import linear
from orchestrator_test_helpers.unit.linear.logs_dir_default_fixtures import (
    LogsDirDefault as LogsDirDefaultBase,
)


class LogsDirDefault1(LogsDirDefaultBase):
    def test_unset_until_an_entry_sets_it(self):
        self.assertIsNone(linear.LOGS_DIR)
