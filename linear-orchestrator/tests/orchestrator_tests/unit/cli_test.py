"""CLI command selection and usage failures; the launchd jobs that run router and promote."""

import io
import json
import os
import plistlib
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from agent_pm.linear_orchestrator import cli
from agent_pm.linear_orchestrator.engine import runs
from agent_pm.utils import Record, Status
from orchestrator_tests.case import REPO, Case
from orchestrator_tests.linear_run_case import SID, ReadyDelegate, RunCase
from orchestrator_tests.unit.streams import Stderr

from delegate.tests.fakes.delegate import FakeDelegate


class Commands(Case):
    def test_help_lists_only_existing_commands(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn(
            "{router,promote,prune,status,show,close,attach-command,resume,writeback}",
            output.getvalue(),
        )

    def test_router_promote_and_prune_help_exits_0_naming_their_options(self):
        for command, options in (
            ("router", ("--now", "--dry-run", "--issue ID", "--brake ROLE")),
            ("promote", ("--dry-run", "--now")),
            ("prune", ("--dry-run",)),
        ):
            for flag in ("--help", "-h"):
                with self.subTest(command=command, flag=flag):
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        self.assertEqual(cli.main([command, flag]), 0)
                    self.assertEqual(errors.getvalue(), "")
                    self.assertIn(f"agent-pm-linear {command}", output.getvalue())
                    for option in options:
                        self.assertIn(option, output.getvalue())

    def test_missing_or_unknown_command_is_usage_error(self):
        for arguments in ([], ["outcome"], ["unknown"]):
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
                    cli.main(arguments)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("agent-pm-linear", output.getvalue())


DEPLOY = "/Users/francis/playground/agent-pm"
LOG = "/Users/francis/.agent-pm/logs/orchestrator.jsonl"
JOBS = {"router": ["--now"], "promote": []}


def plist(job):
    with open(
        os.path.join(REPO, "linear-orchestrator", "launchd", f"com.ophis.agent-pm.{job}.plist"),
        "rb",
    ) as f:
        return plistlib.load(f)


class LaunchdJobs(Case):
    def test_each_job_runs_its_command_from_the_deploy_checkout(self):
        self.assertTrue(os.access(os.path.join(REPO, "bin", "agent-pm-linear"), os.X_OK))
        for job, flags in JOBS.items():
            with self.subTest(job=job):
                job_plist = plist(job)
                self.assertEqual(job_plist["Label"], f"com.ophis.agent-pm.{job}")
                self.assertEqual(
                    job_plist["ProgramArguments"],
                    [f"{DEPLOY}/bin/agent-pm-linear", job, *flags],
                )

    def test_path_finds_claude_tmux_and_homebrew_python_before_the_system_one(self):
        for job in JOBS:
            with self.subTest(job=job):
                path = plist(job)["EnvironmentVariables"]["PATH"].split(":")
                self.assertEqual(
                    path,
                    [
                        "/Users/francis/.local/bin",
                        "/opt/homebrew/bin",
                        "/usr/bin",
                        "/bin",
                        "/usr/sbin",
                        "/sbin",
                    ],
                )

    def test_output_goes_to_the_orchestrator_log(self):
        for job in JOBS:
            with self.subTest(job=job):
                job_plist = plist(job)
                self.assertEqual(
                    {job_plist["StandardOutPath"], job_plist["StandardErrorPath"]}, {LOG}
                )

    def test_router_ticks_on_the_half_hour_and_leaves_its_detached_runs_alive(self):
        router = plist("router")
        self.assertEqual(router["StartCalendarInterval"], [{"Minute": 0}, {"Minute": 30}])
        self.assertTrue(router["AbandonProcessGroup"])
        self.assertNotIn("StartInterval", router)

    def test_promote_runs_every_5_minutes(self):
        promote = plist("promote")
        self.assertEqual(promote["StartInterval"], 300)
        self.assertNotIn("StartCalendarInterval", promote)


class Queries(RunCase):
    def call(self, arguments, *, stdin="", tty=False):
        stdout, stderr = io.StringIO(), Stderr(tty=tty)
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            mock.patch("sys.stdin", io.StringIO(stdin)),
        ):
            result = cli.main(arguments)
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
        self.assertEqual(ReadyDelegate.usage_calls, ["researcher"])

    def test_interrupted_batch_probes_a_shared_recorded_key_once(self):
        self.start("TASK-1")
        self.start("TASK-2")
        FakeDelegate.end(self.cfg.run_dir("TASK-1"))
        FakeDelegate.end(self.cfg.run_dir("TASK-2"))
        result, output, errors = self.call(["resume", "TASK-1", "TASK-2"])
        self.assertEqual((result, errors), (0, ""))
        self.assertEqual(
            [Record.parse(line).name for line in output.splitlines()], ["TASK-1", "TASK-2"]
        )
        self.assertEqual(ReadyDelegate.usage_calls, ["researcher"])

    def test_config_drift_never_probes_and_recorded_limits_still_block(self):
        self.start()
        FakeDelegate.end(self.cfg.run_dir("TASK-1"))
        self.configure(default="other")
        runs.note_limit(
            self.cfg.runs_log,
            issue="TASK-1",
            sid="1234abcd-0000-4000-8000-000000000000",
            client="fake",
            delegate_kind="fake",
            resets_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        )
        for tty in (False, True):
            result, output, errors = self.call(["resume", "TASK-1"], tty=tty)
            self.assertEqual((result, output), (1, ""))
            self.assertEqual(errors, "agent-pm-linear: TASK-1: resume blocked by usage gate\n")
        self.assertEqual(ReadyDelegate.usage_calls, [])
        self.assertFalse(FakeDelegate.attach(self.cfg.run_dir("TASK-1")).alive())

    def test_config_drift_without_a_limit_resumes_and_logs_without_probe(self):
        self.start()
        FakeDelegate.end(self.cfg.run_dir("TASK-1"))
        self.configure(default="other")
        result, output, errors = self.call(["resume", "TASK-1"])
        self.assertEqual((result, errors), (0, ""))
        self.assertEqual(Record.parse(output).name, "TASK-1")
        self.assertEqual(ReadyDelegate.usage_calls, [])
        records = [
            json.loads(line)
            for line in (Path(self.cfg.logs_dir) / "orchestrator.jsonl").read_text().splitlines()
        ]
        self.assertEqual([record["kind"] for record in records], ["usage-drift"])

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
            self.assertTrue(errors.startswith("agent-pm-linear: TASK-2: "))

    def test_invalid_or_duplicate_input_has_no_control_effect(self):
        owner, record = self.start()
        for arguments, stdin in (
            (["close", "TASK-1", "TASK-1"], ""),
            (["close"], record.to_line() + "\n" + record.to_line()),
            (["close"], record.to_line() + "\ninvalid"),
        ):
            with self.subTest(arguments=arguments, stdin=stdin):
                result, output, errors = self.call(arguments, stdin=stdin)
                self.assertEqual((result, output), (2, ""))
                self.assertEqual(len(errors.splitlines()), 1)
                self.assertTrue(owner.alive())
        self.assertEqual(self.call(["close"], stdin=""), (0, "", ""))
        self.assertTrue(owner.alive())

    def test_batch_configuration_error_preserves_valid_key_output(self):
        self.start()
        result, output, errors = self.call(["status", "TASK-8", "TASK-1"])
        self.assertEqual(result, 2)
        self.assertEqual(Status.parse(output).name, "TASK-1")
        self.assertEqual(errors, "agent-pm-linear: TASK-8: TASK-8: no recorded run\n")


class WritebackUsage(RunCase):
    def call(self, arguments, stdin):
        stdout, stderr = io.StringIO(), Stderr(tty=False)
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            mock.patch("sys.stdin", io.StringIO(stdin)),
        ):
            result = cli.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_missing_options_are_usage_errors(self):
        for arguments in (["writeback"], ["writeback", "--issue", "TASK-1"]):
            with self.subTest(arguments=arguments):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    cli.main(arguments)
                self.assertEqual(raised.exception.code, 2)

    def test_what_is_not_one_writable_event_exits_2_without_output(self):
        begun = json.dumps({"v": 1, "kind": "begun", "sid": SID, "attempt": 1})
        other = "9c1d2e3f-4a5b-4c6d-8e7f-a0b1c2d3e4f5"
        workdir = self.cfg.run_dir("TASK-1")
        for name, stdin, workdir_arg, error in (
            ("no event", "", workdir, "want one DelegateEvent line"),
            ("two events", begun + "\n" + begun + "\n", workdir, "want one DelegateEvent line"),
            ("not JSON", "{\n", workdir, "not JSON"),
            (
                "another version",
                begun.replace('"v":1', '"v":2').replace('"v": 1', '"v": 2'),
                workdir,
                "v: want 1",
            ),
            ("an unknown kind", begun.replace("begun", "ended"), workdir, "kind: want one of"),
            ("no start row", begun.replace(SID, other) + "\n", workdir, "no start row"),
            ("another workdir", begun + "\n", self.cfg.run_dir("TASK-2"), "--workdir"),
        ):
            with self.subTest(name):
                result, output, errors = self.call(
                    ["writeback", "--issue", "TASK-1", "--workdir", workdir_arg], stdin
                )
                self.assertEqual((result, output), (2, ""))
                self.assertIn("agent-pm-linear writeback: ", errors)
                self.assertIn(error, errors)
