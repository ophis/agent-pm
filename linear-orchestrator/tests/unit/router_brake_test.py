from datetime import UTC, datetime, timedelta

from agent_pm.linear_orchestrator.engine import config, router
from agent_pm.session import ConfigError
from orchestrator_test_helpers.unit.router.brake_fixtures import Brake as BrakeBase
from orchestrator_test_helpers.unit.router.data import reading


class Brake1(BrakeBase):
    def test_blocks_at_brake_5h(self):
        self.assertEqual(router.BRAKE_5H, 80)
        rows = (
            (reading(70), 0, "five_hour=70% weekly=10%"),
            (reading(79.9, 99.9), 0, "five_hour=79.9% weekly=99.9%"),
            (reading(80), 1, "five_hour=80% weekly=10%"),
            (reading(10, 100), 1, "five_hour=10% weekly=100%"),
            (reading(None, None), 0, "five_hour=none weekly=none"),
            (None, 1, "unknown"),
            (ConfigError("no runtime a"), 1, "unknown"),
            (RuntimeError("boom"), 1, "unknown"),
        )
        for value, rc, out in rows:
            with self.subTest(value=value):
                self.api.usages, self.api.usage_calls = {"a": value}, []
                self.assertEqual(self.brake("researcher"), (rc, out + "\n"))
                self.assertEqual((self.api.usage_calls, self.err), (["a"], ""))
        self.assertEqual(self.path, config.path())

    def test_the_roles_own_runtime(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        self.assertEqual(self.brake("pm"), (0, "five_hour=50% weekly=10%\n"))
        self.assertEqual(self.api.usage_calls, ["b"])

    def test_no_such_role(self):
        self.assertEqual(self.brake("ghost"), (2, ""))
        self.assertEqual((self.err, self.api.usage_calls), ("router.py: no role ghost\n", []))

    def test_a_live_limit_row_is_not_read(self):
        self.limit("a", (datetime.now(UTC) + timedelta(hours=1)).isoformat())
        self.write_runs()
        self.api.usages = {"a": reading(70)}
        self.assertEqual(self.brake("researcher"), (0, "five_hour=70% weekly=10%\n"))
        self.assertEqual(self.api.usage_calls, ["a"])
