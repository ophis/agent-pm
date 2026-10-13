from datetime import timedelta

from agent_pm.session import ConfigError
from orchestrator_test_helpers.unit.router.changed_fixtures import Changed
from orchestrator_test_helpers.unit.router.data import (
    DR,
    EAST,
    NOW,
    PD,
    RECHECK,
    issue,
    reading,
    who,
)
from orchestrator_test_helpers.unit.router.fake_linear_fixtures import FakeLinear, two
from orchestrator_test_helpers.unit.router.usage_gate_fixtures import UsageGate as UsageGateBase


class UsageGate1(UsageGateBase):
    def test_a_blocked_head_runtime_lets_the_next_runtime_claim(self):
        rows = (
            (reading(95, 10), "reason=usage usage=five_hour=95% weekly=10%"),
            (reading(10, 100), "reason=usage usage=five_hour=10% weekly=100%"),
            (None, "reason=usage unknown"),
            (ConfigError("no runtime a"), "reason=usage unknown"),
        )
        for value, why in rows:
            with self.subTest(why=why):
                self.api.usages, self.api.usage_calls = {"a": value, "b": reading(50)}, []
                fake = two()
                self.assertEqual(
                    self.claim(fake, gate=True), f"TASK-2 https://linear.app/x/TASK-2 {PD}"
                )
                self.assertEqual(
                    self.said(),
                    [
                        "pick TASK-1 queue=2",
                        f"usage-skip TASK-1 runtime=a {why}",
                        "pick TASK-2 queue=2",
                        "claim TASK-2 role=pm runtime=b",
                    ],
                )
                self.assertEqual([v["i"] for _, v in fake.mutations], ["TASK-2"])
                self.assertEqual(self.api.usage_calls, ["a", "b"])

    def test_an_absent_window_never_blocks(self):
        self.api.usages = {"a": reading(None, 10)}
        self.assertEqual(
            self.claim(two(), gate=True), "TASK-1 https://linear.app/x/TASK-1 Deep Research"
        )

    def test_two_candidates_of_a_runtime_probe_it_once(self):
        self.api.usages = {"a": reading(95)}
        fake = FakeLinear(
            [
                issue("TASK-1", "Todo", "researcher", priority=1),
                issue("TASK-3", "Todo", "researcher", priority=2),
                issue("TASK-2", "Todo", "pm", priority=3, project=PD),
            ]
        )
        self.assertEqual(self.claim(fake, gate=True), f"TASK-2 https://linear.app/x/TASK-2 {PD}")
        why = "runtime=a reason=usage usage=five_hour=95% weekly=10%"
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=3",
                f"usage-skip TASK-1 {why}",
                "pick TASK-3 queue=3",
                f"usage-skip TASK-3 {why}",
                "pick TASK-2 queue=3",
                "claim TASK-2 role=pm runtime=b",
            ],
        )
        self.assertEqual(self.api.usage_calls, ["a", "b"])

    def test_all_blocked_claims_nothing(self):
        self.api.usages = {"a": reading(95), "b": None}
        fake = two()
        self.assertEqual(self.claim(fake, gate=True), "")
        self.assertEqual(fake.mutations, [])
        self.assertEqual(self.said()[-1], "pick-none reason=nothing claimable")

    def test_the_claim_gates_on_the_rechecked_roles_runtime(self):
        self.api.usages = {"a": reading(95), "b": reading(50)}
        rows = (
            ("researcher", "pm", f"TASK-1 https://linear.app/x/TASK-1 {DR}", ["b"]),
            ("pm", "researcher", "", ["a"]),
        )
        for listed, now, out, calls in rows:
            with self.subTest(listed=listed):
                self.api.usage_calls = []
                fake = Changed(
                    [issue("TASK-1", "Todo", listed)], {"TASK-1": {"assignee": who(now)}}
                )
                self.assertEqual(self.claim(fake, gate=True), out)
                self.assertEqual(self.api.usage_calls, calls)
                self.assertEqual(len(fake.mutations), int(bool(out)))

    def test_a_usage_skip_is_said_once_a_day(self):
        self.api.usages = {"a": reading(95)}
        fake = FakeLinear([issue("TASK-1", "Todo", "researcher")])
        skip = "usage-skip TASK-1 runtime=a reason=usage usage=five_hour=95% weekly=10%"
        for again, said in ((False, [skip]), (True, [])):
            self.assertEqual(self.claim(fake, gate=True, again=again), "")
            self.assertEqual(
                self.said(), ["pick TASK-1 queue=1", *said, "pick-none reason=nothing claimable"]
            )
        self.assertEqual(self.api.usage_calls, ["a", "a"])

    def test_dry_run_prints_the_usage_of_each_runtime(self):
        at = (NOW + timedelta(hours=1)).astimezone(EAST).isoformat()
        self.limit("a", at)
        fake = two()
        self.assertEqual(
            self.claim(fake, dry=True, gate=True), f"TASK-2 https://linear.app/x/TASK-2 {PD}"
        )
        self.assertEqual(
            self.said(),
            [
                "pick TASK-1 queue=2",
                f"usage runtime=a until={at} allowed=false",
                f"usage-skip TASK-1 runtime=a reason=limit until={at}",
                "pick TASK-2 queue=2",
                "usage runtime=b usage=five_hour=10% weekly=10% allowed=true",
            ],
        )
        self.assertEqual(self.taken[1:], (None, "pm", "b"))
        self.assertEqual((fake.mutations, self.api.usage_calls), ([], ["b"]))
        self.assertNotIn(RECHECK, [q for q, _ in fake.queries])
