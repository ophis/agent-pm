"""CLI command selection and usage failures."""

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_pm.linear_orchestrator import cli
from agent_pm.utils import Record, Status
from orchestrator_tests.case import Case
from orchestrator_tests.linear_run_case import RunCase

from delegate.tests.fakes.delegate import FakeDelegate


class Commands(Case):
    def test_help_lists_only_existing_commands(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn(
            "{router,promote,run,status,show,close,attach-command,resume}", output.getvalue()
        )
        self.assertIn("legacy entry, replaced by TASK-374", output.getvalue())

    def test_missing_or_unknown_command_is_usage_error(self):
        for arguments in ([], ["outcome"], ["unknown"]):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
                    cli.main(arguments)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("agent-pm-linear", output.getvalue())


class Queries(RunCase):
    def call(self, arguments, *, stdin=""):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            mock.patch("sys.stdin", io.StringIO(stdin)),
        ):
            result = cli.main(arguments, cfg=self.cfg)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_status_for_two_issue_keys_or_stdin_records_has_two_ordered_lines(self):
        _, first = self.start("TASK-1")
        _, second = self.start("TASK-2")
        for arguments, stdin in (
            (["status", "TASK-2", "TASK-1"], ""),
            (["status"], second.to_line() + "\n" + first.to_line() + "\n"),
        ):
            with self.subTest(arguments=arguments):
                result, output, errors = self.call(arguments, stdin=stdin)
                statuses = [Status.parse(line) for line in output.splitlines()]
                self.assertEqual((result, errors), (0, ""))
                self.assertEqual([status.name for status in statuses], ["TASK-2", "TASK-1"])
                self.assertTrue(all(status.alive for status in statuses))
                self.assertEqual([status.attach for status in statuses], [None, None])

    def test_live_completed_and_interrupted_resume_keep_record_and_start_once(self):
        _, record = self.start()
        for state in ("live", "completed", "interrupted", "live-again"):
            if state == "completed":
                FakeDelegate.finish(record.workdir)
            if state == "interrupted":
                FakeDelegate.end(record.workdir)
            result, output, errors = self.call(["resume", "TASK-1"])
            self.assertEqual((result, errors), (0, ""))
            self.assertEqual(Record.parse(output), record)
            with open(record.workdir + "/delegate.json", encoding="utf-8") as file:
                self.assertEqual(
                    json.load(file)["starts"], 2 if state in ("interrupted", "live-again") else 1
                )

    def test_close_controls_only_selected_issue_and_show_failure_names_it(self):
        one, _ = self.start("TASK-1")
        two, _ = self.start("TASK-2")
        result, output, errors = self.call(["close", "TASK-1"])
        self.assertEqual((result, output, errors), (0, "", ""))
        self.assertFalse(one.alive())
        self.assertTrue(two.alive())
        for command in ("show", "attach-command"):
            result, output, errors = self.call([command, "TASK-2"])
            self.assertEqual((result, output), (1, ""))
            self.assertEqual(len(errors.splitlines()), 1)
            self.assertTrue(errors.startswith("TASK-2: "))

    def test_invalid_or_duplicate_input_has_no_control_effect(self):
        owner, record = self.start()
        for arguments, stdin in (
            (["close", "TASK-1", "TASK-1"], ""),
            (["close"], record.to_line() + "\n" + record.to_line()),
            (["close"], record.to_line() + "\ninvalid"),
            (["close"], ""),
        ):
            with self.subTest(arguments=arguments, stdin=stdin):
                result, output, errors = self.call(arguments, stdin=stdin)
                self.assertEqual((result, output), (2, ""))
                self.assertEqual(len(errors.splitlines()), 1)
                self.assertTrue(owner.alive())

    def test_batch_configuration_error_preserves_valid_key_output(self):
        self.start()
        result, output, errors = self.call(["status", "TASK-8", "TASK-1"])
        self.assertEqual(result, 2)
        self.assertEqual(Status.parse(output).name, "TASK-1")
        self.assertEqual(errors, "TASK-8: TASK-8: no recorded run\n")
