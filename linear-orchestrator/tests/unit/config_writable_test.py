from agent_pm.linear_orchestrator.engine import config
from agent_pm.utils import temp_dirs
from orchestrator_test_helpers.unit.config.writable_fixtures import Writable as WritableBase


class Writable1(WritableBase):
    def test_work_then_the_temp_dirs(self):
        self.assertEqual(config.writable("/w"), ("/w", *temp_dirs()))
        with self.assertRaises(TypeError):
            config.writable()
