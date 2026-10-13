import argparse
import os

from agent_pm.linear_orchestrator.engine import router, runs
from orchestrator_test_helpers.unit.clock import frozen
from orchestrator_test_helpers.unit.router.begin_fixtures import Begin as BeginBase
from orchestrator_test_helpers.unit.router.data import DR, IDS, ROLE, issue


class Begin1(BeginBase):
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
