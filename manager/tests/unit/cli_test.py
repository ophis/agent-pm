"""Crew CLI lease identity validation and holder command shape."""

import contextlib
import io
import os
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_pm.manager import Spec
from agent_pm.manager.cli import main
from agent_pm.utils import Status

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

    def test_missing_entry_is_usage_error_without_controller_or_writes(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("missing ctl")) as run:
            for argv in (("stop",), ("status",), ("forget",), ("resume",), ("forget", "--force")):
                with self.subTest(argv=argv):
                    command, *extra = argv
                    result, stdout, stderr = self.invoke(
                        command, "missing", "--manager", "mgr", *extra
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


class NextEventContent(CliCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.answers = {}

    def launched(self, name, *sessions, record_name=None):
        sid = str(uuid.uuid4())
        record = replace(
            self.fake_record(record_name or name, sid=sid), sessions=sessions or (name,)
        )
        self.roster.begin(Spec(name, ("start",)), sid=sid)
        self.roster.commit(name, record)
        return record

    def next_event(self, event, *extra):
        Path(self.roster.events).write_text(f"{event}\n")

        def run(argv, **_):
            self.calls.append(tuple(argv))
            code, out, err = self.answers.get(tuple(argv), (1, "", "no such session"))
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)

        with mock.patch("subprocess.run", side_effect=run):
            return self.invoke(
                "next-event", "--manager", "mgr", "--after", "0", "--gen", "0", *extra
            )

    def test_without_content_prints_only_the_event_line(self):
        self.launched("w1")
        self.assertEqual(self.next_event("12:00:00 w1 done"), (0, "1 12:00:00 w1 done\n", ""))
        self.assertEqual(self.calls, [])

    def test_done_prints_the_sessions_reply_and_blocked_its_screen(self):
        for sessions, ask, event, answer, expected in (
            (("w1",), ("reply", "w1"), "w1 done", "the answer\n", "the answer\n"),
            (
                ("test-drive",),
                ("reply", "test-drive"),
                "test-drive done",
                "the answer\n",
                "the answer\n",
            ),
            (
                ("w1",),
                ("read", "w1"),
                "w1 blocked",
                "Allow this edit?\n 1. Yes",
                "Allow this edit?\n 1. Yes\n",
            ),
        ):
            with self.subTest(sessions=sessions, event=event):
                self.launched("w1", *sessions)
                self.answers = {("agent-pm-session", *ask): (0, answer, "")}
                self.assertEqual(
                    self.next_event(f"12:00:00 {event}", "--content"),
                    (0, f"1 12:00:00 {event}\n{expected}", ""),
                )
                self.roster.remove("w1")

    def test_a_harness_event_prints_its_entry_status(self):
        for sessions, record_name in (
            (("engineer-ab12-drive",), "engineer-ab12"),
            (("engineer-ab12", "engineer-ab12-drive"), None),
        ):
            with self.subTest(sessions=sessions):
                record = self.launched("r1", *sessions, record_name=record_name)
                status = Status(
                    record.name, alive=False, state="exited", outcome="done", attach=None
                )
                self.answers = {(*record.ctl, "status"): (0, status.to_line() + "\n", "")}
                self.assertEqual(
                    self.next_event("12:00:00 engineer-ab12-drive outcome done", "--content"),
                    (
                        0,
                        "1 12:00:00 engineer-ab12-drive outcome done\n"
                        f"{replace(status, name='r1').to_line()}\n",
                        "",
                    ),
                )
                self.assertNotIn(("agent-pm-session", "read", "engineer-ab12-drive"), self.calls)
                self.roster.remove("r1")

    def test_an_event_naming_only_control_characters_still_exits_0(self):
        self.launched("w1")
        result, stdout, _ = self.next_event("12:00:00 \x01 done", "--content")
        self.assertEqual(result, 0)
        head, content = stdout.split("\n", 1)
        self.assertEqual(head, "1 12:00:00  done")
        self.assertTrue(content.startswith("content unavailable: "), content)
        self.assertEqual(self.calls, [])

    def test_unreadable_content_still_prints_the_event_and_says_why(self):
        self.launched("w1")
        for event in ("12:00:00 gone dead", "12:00:00 gone done", "12:00:00 gone-drive exited 0"):
            with self.subTest(event=event):
                result, stdout, _ = self.next_event(event, "--content")
                self.assertEqual(result, 0)
                head, content = stdout.split("\n", 1)
                self.assertEqual(head, f"1 {event}")
                self.assertTrue(content.startswith("content unavailable: "), content)
                self.assertTrue(content.endswith("\n") and content.count("\n") == 1, content)


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
