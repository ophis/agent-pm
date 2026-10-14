"""CLI command selection and usage failures."""

import io
from contextlib import redirect_stderr, redirect_stdout

from agent_pm.linear_orchestrator import cli
from orchestrator_tests.case import Case


class Commands(Case):
    def test_help_lists_only_existing_commands(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("{router,promote,run}", output.getvalue())

    def test_missing_or_unknown_command_is_usage_error(self):
        for arguments in ([], ["status"], ["unknown"]):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
                    cli.main(arguments)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("agent-pm-linear", output.getvalue())
