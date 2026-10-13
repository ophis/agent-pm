import os

from orchestrator_test_helpers.unit.promote.test_idle_fixtures import TestIdle as TestIdleBase


class TestIdle1(TestIdleBase):
    def test_two_runs_without_handoff_log_nothing(self):
        self.fake.add("DR-1", state="In Review")
        for _ in range(2):
            self.assertEqual(self.run_main(), 0)
            self.assertEqual(self.out, "")
        self.assertFalse(os.path.exists(self.logs))
