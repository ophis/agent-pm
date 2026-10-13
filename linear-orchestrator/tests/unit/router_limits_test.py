from datetime import timedelta

from orchestrator_test_helpers.unit.router.data import EAST, NOW, PD, issue
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear, two
from orchestrator_test_helpers.unit.router.limits_fixtures import Limits as LimitsBase


class Limits1(LimitsBase):
    def test_a_live_limit_row_skips_its_runtime_without_a_probe(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        self.limit("a", at)
        fake = two()
        self.assertEqual(self.claim(fake, gate=True), f"TASK-2 https://linear.app/x/TASK-2 {PD}")
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=2",
                f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                "pick TASK-2 queue=2",
                "claim TASK-2 role=pm runtime=b",
            ],
        )
        self.assertEqual(self.api.usage_calls, ["b"])

    def test_a_limit_row_not_live_changes_nothing(self):
        rows = (
            (NOW - timedelta(minutes=1)).isoformat(),
            None,
            (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat(),
            5,
            "soon",
            (NOW + timedelta(days=8)).isoformat(),
        )
        for resets_at in rows:
            with self.subTest(resets_at=resets_at):
                self.lines, self.api.usage_calls = [], []
                self.limit("a", resets_at)
                fake = two()
                self.assertEqual(
                    self.claim(fake, gate=True), "TASK-1 https://linear.app/x/TASK-1 Deep Research"
                )
                self.assertEqual(self.api.usage_calls, ["a"])

    def test_limit_rows_are_no_attempts(self):
        for sid in "abc":
            self.add("start", "TASK-1", sid, 300)
        for _ in range(4):
            self.limit("b", (NOW + timedelta(hours=1)).isoformat(), ident="TASK-1")
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        self.assertEqual(self.claim(fake), "TASK-1 https://linear.app/x/TASK-1 Deep Research")
