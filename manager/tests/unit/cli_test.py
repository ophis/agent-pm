"""Crew CLI lease identity validation and holder command shape."""

import contextlib
import io
import os
import uuid
from unittest import mock

from agent_pm.manager.cli import main

from manager.tests.cli_case import CliCase


class CliTest(CliCase):
    def test_invalid_or_partial_holder_environment_is_usage_error_without_writes(self):
        identity = str(uuid.uuid4())
        for env in (
            {"AGENT_PM_HOLDER_ID": identity},
            {"AGENT_PM_HOLDER_GENERATION": "1"},
            {"AGENT_PM_HOLDER_ID": "", "AGENT_PM_HOLDER_GENERATION": "1"},
            {"AGENT_PM_HOLDER_ID": "invalid", "AGENT_PM_HOLDER_GENERATION": "1"},
            {"AGENT_PM_HOLDER_ID": str(uuid.uuid1()), "AGENT_PM_HOLDER_GENERATION": "1"},
            {"AGENT_PM_HOLDER_ID": identity.upper(), "AGENT_PM_HOLDER_GENERATION": "1"},
            {"AGENT_PM_HOLDER_ID": identity, "AGENT_PM_HOLDER_GENERATION": "0"},
            {"AGENT_PM_HOLDER_ID": identity, "AGENT_PM_HOLDER_GENERATION": "-1"},
            {"AGENT_PM_HOLDER_ID": identity, "AGENT_PM_HOLDER_GENERATION": "01"},
            {"AGENT_PM_HOLDER_ID": identity, "AGENT_PM_HOLDER_GENERATION": "text"},
        ):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                result, stdout, stderr = self.invoke("attach", "--manager", "mgr")
                self.assertEqual(result, 2, stderr)
                self.assertEqual(stdout, "")
                self.assertTrue(stderr.startswith("crew: "))
                self.assertFalse(os.path.exists(self.directory))

    def test_attach_uses_holder_generation_and_does_not_take_new_lease(self):
        with self.roster.hold("label") as lease:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "AGENT_PM_HOLDER_ID": lease.id,
                        "AGENT_PM_HOLDER_GENERATION": str(lease.generation),
                    },
                ),
                self.listed(),
            ):
                result, _, stderr = self.invoke("attach", "--manager", "mgr")
            self.assertEqual(result, 0, stderr)
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "AGENT_PM_HOLDER_ID": lease.id,
                        "AGENT_PM_HOLDER_GENERATION": str(lease.generation + 1),
                    },
                ),
                self.listed(),
            ):
                result, _, stderr = self.invoke("attach", "--manager", "mgr")
            self.assertEqual(result, 1, stderr)

    def test_unleased_attach_cannot_access_live_holder(self):
        with self.roster.hold("label"), self.listed():
            result, _, stderr = self.invoke("attach", "--manager", "mgr")
        self.assertEqual(result, 1, stderr)

    def test_removed_owner_and_missing_holder_command_are_usage_errors(self):
        for argv in (
            ("attach", "--manager", "mgr", "--owner", "label"),
            ("hold", "--manager", "mgr"),
            ("hold", "--manager", "mgr", "--"),
            ("attach", "--manager", "mgr", "--", "unused"),
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(argv)
                self.assertEqual(error.exception.code, 2)
        self.assertFalse(os.path.exists(self.directory))

    def test_missing_entry_is_usage_error_for_every_named_control_command(self):
        for command in ("stop", "status", "forget", "resume"):
            with self.subTest(command=command):
                result, stdout, stderr = self.invoke(command, "missing", "--manager", "mgr")
                self.assertEqual(result, 2, stderr)
                self.assertEqual(stdout, "")
                self.assertIn("not in roster", stderr)
                self.assertFalse(os.path.exists(self.directory))

    def test_force_forget_missing_entry_is_usage_error_without_controller_or_writes(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("missing ctl")) as run:
            for extra in ((), ("--force",)):
                with self.subTest(extra=extra):
                    result, stdout, stderr = self.invoke(
                        "forget", "missing", "--manager", "mgr", *extra
                    )
                    self.assertEqual(result, 2, stderr)
                    self.assertEqual(stdout, "")
                    self.assertIn("not in roster", stderr)
                    self.assertFalse(os.path.exists(self.directory))
        run.assert_not_called()

    def test_empty_stop_all_reports_nothing_without_creating_a_manager(self):
        with self.listed():
            result, stdout, stderr = self.invoke("stop-all", "--manager", "mgr")
        self.assertEqual(result, 0, stderr)
        self.assertEqual(stdout, "nothing to stop\n")
        self.assertFalse(os.path.exists(self.directory))


class InsideAnAgentSession(CliCase):
    def test_run_hold_and_resume_exit_2_and_write_nothing(self):
        for argv in (
            ("run", "--manager", "mgr", "w1", "--", "true"),
            ("hold", "--manager", "mgr", "--", "true"),
            ("resume", "--manager", "mgr", "w1"),
        ):
            with (
                self.subTest(argv=argv),
                mock.patch.dict(os.environ, {"AGENT_PM_AGENT_SESSION": "1"}),
            ):
                result, stdout, stderr = self.invoke(*argv)
                self.assertEqual((result, stdout), (2, ""))
                self.assertTrue(stderr.startswith("crew: refused inside an agent session"), stderr)
                self.assertFalse(os.path.exists(self.directory))

    def test_help_and_control_commands_still_run(self):
        with mock.patch.dict(os.environ, {"AGENT_PM_AGENT_SESSION": "1"}):
            with (
                self.assertRaises(SystemExit) as raised,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main(["--help"])
            self.assertEqual(raised.exception.code, 0)
            self.assertNotIn("refused", self.invoke("status", "--manager", "mgr")[2])
