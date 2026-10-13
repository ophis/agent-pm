import io
from contextlib import redirect_stderr

from agent_pm.linear_orchestrator.engine import router
from agent_pm.session import ConfigError
from orchestrator_test_helpers.unit.router.data import reading
from orchestrator_test_helpers.unit.router.gate_fixtures import Gate as GateBase


class Gate1(GateBase):
    def test_thresholds(self):
        self.assertEqual((router.MAX_5H, router.BRAKE_5H, router.MAX_WEEK), (90, 80, 100))
        rows = (
            (reading(89.9, 99.9), None),
            (reading(90, 10), "reason=usage usage=five_hour=90% weekly=10%"),
            (reading(10, 100), "reason=usage usage=five_hour=10% weekly=100%"),
            (reading(150, None), "reason=usage usage=five_hour=150% weekly=none"),
            (reading(None, 99), None),
            (reading(89, None), None),
            (reading(None, None), None),
            (None, "reason=usage unknown"),
            (ConfigError("no runtime a"), "reason=usage unknown"),
            (RuntimeError("boom"), "reason=usage unknown"),
        )
        for value, why in rows:
            with self.subTest(value=value):
                self.api.usages = {"a": value}
                with redirect_stderr(io.StringIO()), self.log_dir(False):
                    self.assertEqual(self.gate().blocked("a", "TASK-1"), why is not None)
                self.raw = ""
                self.assertEqual(self.said(), [f"usage-skip TASK-1 runtime=a {why}"] if why else [])

    def test_one_probe_per_runtime(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        with redirect_stderr(io.StringIO()), self.log_dir(False):
            gate = self.gate()
            got = [
                gate.blocked(r, i) for r, i in (("a", "TASK-1"), ("b", "TASK-2"), ("a", "TASK-3"))
            ]
        self.assertEqual((got, self.api.usage_calls), ([True, False, True], ["a", "b"]))

    def test_summary(self):
        rows = (
            (None, "unknown"),
            (reading(95, 10.5), "five_hour=95% weekly=10.5%"),
            (reading(None, 100), "five_hour=none weekly=100%"),
            (reading(89.94, None), "five_hour=89.94% weekly=none"),
        )
        for usage, out in rows:
            self.assertEqual(router.summary(usage), out)
