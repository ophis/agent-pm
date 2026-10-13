"""Crew CLI lease identity validation and holder command shape."""

import contextlib
import io
import os
import uuid
from unittest import mock

from agent_pm.manager import Crew, Spec
from agent_pm.manager.cli import main
from agent_pm.manager.engine.roster import Roster

from manager.tests.case import Case


class CliTest(Case):
    def setUp(self):
        self.directory = os.path.join(self.agent_pm, "managers", "mgr")
        self.roster = Roster(self.directory)
        env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_PM_")}
        self.enterContext(mock.patch.dict(os.environ, env, clear=True))

    def invoke(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

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

    def test_force_forget_is_explicit_and_needs_no_controller(self):
        command = self.setup_ctl()
        Crew(self.roster).run(Spec("worker", (command, "start", "worker")))
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("missing ctl")):
            result, _, stderr = self.invoke("forget", "worker", "--manager", "mgr")
            self.assertEqual(result, 1, stderr)
            self.assertIn("worker", self.roster.entries())
            result, _, stderr = self.invoke("forget", "worker", "--manager", "mgr", "--force")
            self.assertEqual(result, 0, stderr)
        self.assertEqual(self.roster.entries(), {})

    def test_empty_run_note_and_note_command_store_null(self):
        command = self.setup_ctl()
        result, _, stderr = self.invoke(
            "run", "worker", "--manager", "mgr", "--note", "", "--", command, "start", "worker"
        )
        self.assertEqual(result, 0, stderr)
        self.assertIsNone(self.roster.entries()["worker"].note)
        result, _, stderr = self.invoke("note", "worker", "kept", "--manager", "mgr")
        self.assertEqual(result, 0, stderr)
        self.assertEqual(self.roster.entries()["worker"].note, "kept")
        result, _, stderr = self.invoke("note", "worker", "", "--manager", "mgr")
        self.assertEqual(result, 0, stderr)
        self.assertIsNone(self.roster.entries()["worker"].note)

    def test_missing_entry_is_usage_error_for_every_named_control_command(self):
        for command in ("stop", "status", "forget", "resume"):
            with self.subTest(command=command):
                result, stdout, stderr = self.invoke(command, "missing", "--manager", "mgr")
                self.assertEqual(result, 2, stderr)
                self.assertEqual(stdout, "")
                self.assertIn("not in roster", stderr)
                self.assertFalse(os.path.exists(self.directory))

    def test_empty_stop_all_reports_nothing_without_creating_a_manager(self):
        with self.listed():
            result, stdout, stderr = self.invoke("stop-all", "--manager", "mgr")
        self.assertEqual(result, 0, stderr)
        self.assertEqual(stdout, "nothing to stop\n")
        self.assertFalse(os.path.exists(self.directory))
