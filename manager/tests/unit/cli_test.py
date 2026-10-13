"""Crew CLI lease identity validation and holder command shape."""

import contextlib
import io
import os
import uuid
from unittest import mock

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
