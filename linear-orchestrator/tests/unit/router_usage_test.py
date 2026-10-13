from agent_pm.linear_orchestrator.engine import router
from orchestrator_test_helpers.unit.router.usage_fixtures import Usage as UsageBase


class Usage1(UsageBase):
    def test_unknown_flags_rejected(self):
        for argv in (
            ["--help"],
            ["-h"],
            ["--prune", "x"],
            ["--gate", "new"],
            ["--now", "--issue", "TASK-1"],
            ["--issue", "TASK-1", "--now"],
            ["--brake"],
            ["--brake", "--dry-run"],
            ["--brake", "--brake"],
            ["--dry-run", "--brake", "researcher"],
            ["--brake", "researcher", "--now"],
            ["--brake", "researcher", "pm"],
            ["--brake=researcher"],
            ["--issue", "TASK-1", "--brake", "researcher"],
            ["--tui"],
            ["--now", "--tui"],
            ["--issue", "TASK-1", "--tui"],
            ["--split", "right"],
            ["--split-from", "dev"],
            ["--events", "x"],
            ["--manager", "m1"],
            ["--now=x"],
            ["--issue=TASK-1"],
            ["--now", "--issue=TASK-1"],
            ["--issue", "TASK-1", "--issue", "TASK-2"],
            ["--issue"],
            ["--now", "--issue", "--dry-run"],
            *([f, "x"] for f in ("--uuid", "--target", "--project", "--assignee", "--sid")),
            *([f, "x"] for f in ("--task", "--mode", "--runtime", "--input", "--runner")),
            ["--issue", "TASK-1", "--runtime", "a"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.main(argv), (2, router.USAGE + "\n"))
        self.assertEqual((self.api.order, self.api.usage_calls), ([], []))
        self.assertEqual(
            router.USAGE,
            "usage: python3 -P -m agent_pm.linear_orchestrator.engine.router "
            "[--now] [--dry-run] [--issue ID] | --brake ROLE",
        )
        for flag in ("--gate", "--tui", "--split", "--events", "--manager"):
            self.assertNotIn(flag, router.USAGE + router.__doc__)

    def test_bad_issue_id(self):
        for argv in (
            ["--issue", "task-7"],
            ["--issue", "TASK"],
            ["--dry-run", "--issue", "TASK-7x"],
        ):
            with self.subTest(argv=argv):
                ident = argv[argv.index("--issue") + 1]
                self.assertEqual(self.main(argv), (2, f"router.py: bad issue id: {ident}\n"))
