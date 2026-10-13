import argparse
import os
from datetime import timedelta

from agent_pm.linear_orchestrator.engine import router, runs
from orchestrator_tests.unit.clock import frozen
from orchestrator_tests.unit.router_data import (
    DR,
    EAST,
    IDS,
    NOW,
    PD,
    ROLE,
    SESSIONS,
    issue,
)
from orchestrator_tests.unit.router_fakes import (
    FakeLinear,
    two,
)
from orchestrator_tests.unit.router_scenarios import (
    Base,
)


class Limits(Base):
    """FR-60: a live limit row blocks its runtime with no probe; any other limit row changes
    nothing.
    """

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


class LiveSessions(Base):
    def test_exact_names_of_the_roles(self):
        self.api.live = [
            "agent-pm-engineer-TASK-2",
            "agent-pm-engineer-TASK-10",
            "agent-pm-researcher-TASK-1",
            "agent-pm-pm-TASK-6",
            "agent-pm-pm-lead-TASK-4",
            "agent-pm-engineer",
            "agent-pm-engineer-TASK-1x",
            "agent-pm-engineer-task-3",
            "agent-pm-engineer-TASK-",
            "agent-pm-ghost-TASK-5",
            "engineer-TASK-7-0b6f2c1e",
            "other",
        ]
        self.assertEqual(
            router.live_sessions(["researcher", "pm", "pm-lead", "engineer"], driver=self.api),
            {
                "researcher": ["TASK-1"],
                "pm": ["TASK-6"],
                "pm-lead": ["TASK-4"],
                "engineer": ["TASK-10", "TASK-2"],
            },
        )
        self.assertEqual(self.api.order, SESSIONS)

    def test_unknown_sessions_are_none(self):
        self.api.live = None
        self.assertIsNone(router.live_sessions(["researcher", "engineer"], driver=self.api))


class Begin(Base):
    """FR-46: the start or resume line records the runtime; the launch Namespace carries it."""

    def test_one_json_line_and_the_launch_namespace(self):
        path = os.path.join(self.tmp, "logs", "runs.jsonl")
        found = issue("TASK-1", "In Progress", "researcher")
        with frozen("2026-10-09T00:26:33-04:00"):
            got = [
                router.begin(mode, found, "s1", "researcher", task, "a", path)
                for mode, task in (("new", "light-research"), ("resume", None))
            ]
        with open(path) as f:
            self.assertEqual(
                f.read(),
                "".join(
                    f'{{"ts": "2026-10-09T00:26:33-04:00", "kind": "{kind}", "issue": "TASK-1", '
                    '"sid": "s1", "role": "researcher", "runtime": "a"}\n'
                    for kind in ("start", "resume")
                ),
            )
        self.assertEqual(
            got,
            [
                argparse.Namespace(
                    issue="TASK-1",
                    project=IDS[DR],
                    assignee=ROLE["researcher"],
                    sid="s1",
                    task=task,
                    mode=mode,
                    runtime="a",
                )
                for mode, task in (("new", "light-research"), ("resume", None))
            ],
        )
        self.assertEqual([e.kind for e in runs.parse_log(path)], ["start", "resume"])
