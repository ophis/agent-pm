"""run.py's argument parsing and identity validation."""

import contextlib
import io
from dataclasses import replace

from agent_pm.linear_orchestrator.engine.types import Spec
from orchestrator_tests.case import Case


class RunSpec(Case):
    def spec(self, **overrides):
        return replace(
            Spec(
                uuid="00000000-0000-4000-8000-000000000001",
                issue="TASK-26",
                project="",
                assignee="engineer@example.com",
                sid="00000000-0000-4000-8000-000000000002",
                mode="new",
                client="claude",
                input="/tmp/run input.txt",
            ),
            **overrides,
        )

    def test_parser_keeps_required_arguments_and_mode_validation(self):
        invalid = [
            *("--uuid", "00000000-0000-4000-8000-000000000001", "--issue=TASK-26", "--project="),
            *("--assignee=engineer@example.com", "--sid=00000000-0000-4000-8000-000000000002"),
            *("--mode=invalid", "--client=claude", "--input=x"),
        ]
        for argv in ([], invalid):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    Spec.from_argv(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_identity_and_role_validation(self):
        spec = self.spec()
        self.assertIsNone(spec.identity_error())
        self.assertIsNone(spec.role_error("engineer", ("build",)))
        self.assertEqual(
            self.spec(issue="bad", sid="bad").identity_error(),
            "bad issue or session id: bad bad",
        )
        self.assertEqual(spec.role_error(None, ()), "'engineer@example.com' is not a role account")
        self.assertEqual(
            self.spec(task="unknown").role_error("engineer", ("build",)),
            "task 'unknown' is not one of engineer's tasks (build)",
        )
