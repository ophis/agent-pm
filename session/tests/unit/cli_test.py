import contextlib
import io
import json
import sys
from dataclasses import replace
from unittest import mock

from agent_pm.session import cli
from agent_pm.session.engine.hosted_session import HostedSession
from agent_pm.utils import Record

from session.tests.case import Case
from session.tests.hosted_support import SID, HostedTuiBase


class ReservedName(Case):
    def test_none_as_a_session_name_is_a_usage_error(self):
        self.addCleanup(setattr, HostedSession, "_setup", None)
        for argv in (["start", "none", "--client", "claude"], ["hide", "none"], ["close", "none"]):
            err = io.StringIO()
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(err),
                self.assertRaises(SystemExit) as caught,
            ):
                cli.main(argv)
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("want [A-Za-z0-9_-]+ but not none, not 'none'", err.getvalue())


class RecordControl(HostedTuiBase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(cli, "Session", self.session))
        self.enterContext(
            mock.patch.object(cli, "_configure", return_value=self.session._config().hosts)
        )
        self.record = Record(
            v=1,
            sid=SID,
            kind="worker",
            name="w1",
            sessions=("w1",),
            workdir=None,
            display=None,
            detail=None,
            ctl=("agent-pm-session",),
            resume=("agent-pm-session", "resume", "w1"),
        )
        self.host.calls.attach_command.return_value = "attach-session"

    def invoke(self, *argv, record=None, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        source = stdin if stdin is not None else "" if record is None else record.to_line() + "\n"
        with (
            mock.patch.object(sys, "stdin", io.StringIO(source)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_record_sid_mismatch_command_results(self):
        replacement = self.host.create("w1", sid="replacement")
        for command in ("status", "close", "show", "hide", "attach-command", "content"):
            with self.subTest(command=command):
                code, out, err = self.invoke(command, record=self.record)
                if command == "status":
                    self.assertEqual(code, 0)
                    self.assertFalse(json.loads(out)["alive"])
                elif command == "close":
                    self.assertEqual((code, out), (0, ""))
                else:
                    self.assertEqual((code, out), (1, ""))
                    self.assertIn(SID, err)
                    self.assertIn("replacement", err)
        replacement.calls.close.assert_not_called()
        self.host.calls.show.assert_not_called()
        self.host.calls.hide.assert_not_called()
        self.host.calls.attach_command.assert_not_called()

    def test_matching_private_sid_allows_close_without_record_metadata(self):
        process = self.host.create("w1", sid=SID)
        self.assertIsNone(process.record())
        self.assertEqual(self.invoke("close", record=self.record)[:2], (0, ""))
        self.assertNotIn("w1", self.host.processes)

    def test_all_record_sessions_are_checked_before_effects(self):
        first = self.host.create("w1", sid=SID)
        replacement = self.host.create("w2", sid="replacement")
        record = replace(self.record, sessions=("w1", "w2"))
        for command in ("show", "hide", "close"):
            with self.subTest(command=command):
                self.invoke(command, record=record)
        first.calls.close.assert_not_called()
        replacement.calls.close.assert_not_called()
        self.host.calls.show.assert_not_called()
        self.host.calls.hide.assert_not_called()

    def test_duplicate_keys_refuse_before_close(self):
        process = self.host.create("w1", sid=SID)
        self.assertEqual(self.invoke("close", "w1", "w1")[:2], (2, ""))
        process.calls.close.assert_not_called()

    def test_content_takes_exactly_one_key(self):
        self.host.create("w1", sid=SID)
        self.host.create("w2", sid=SID)
        code, out, err = self.invoke("content", "w1", "w2")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("exactly one", err)

    def test_sid_option_rejects_reused_names_without_effects(self):
        self.tui(name="w1", sid=SID)
        replacement = self.host.create("w1", sid="replacement")
        for argv in (
            ("send", "w1", "hello"),
            ("send", "w1", "--key", "Enter"),
            ("reply", "w1"),
            ("read", "w1"),
        ):
            with self.subTest(argv=argv):
                code, out, err = self.invoke(*argv, "--sid", SID)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(SID, err)
                self.assertIn("replacement", err)
        replacement.calls.send.assert_not_called()
        replacement.calls.press.assert_not_called()
        self.host.calls.screen.assert_not_called()

    def test_read_keeps_name_only_host_terminals(self):
        self.host.create("plain")
        self.host.calls.screen.return_value = "host screen"
        self.assertEqual(self.invoke("read", "plain")[:2], (0, "host screen\n"))

    def test_matching_sid_allows_send_reply_read(self):
        self.tui(name="w1", sid=SID)
        self.host.calls.screen.return_value = "screen"
        self.assertEqual(self.invoke("send", "w1", "hello", "--sid", SID)[:2], (0, ""))
        self.assertEqual(self.invoke("reply", "w1", "--sid", SID)[:2], (0, ""))
        self.assertEqual(self.invoke("read", "w1", "--sid", SID)[:2], (0, "screen\n"))

    def test_malformed_record_is_usage_error(self):
        self.assertEqual(self.invoke("status", stdin="{}\n")[:2], (2, ""))

    def test_content_record_uses_session_target(self):
        self.tui(name="w1", sid=SID)
        self.host.calls.screen.return_value = "screen"
        record = replace(self.record, name="outer")
        self.assertEqual(self.invoke("content", record=record)[:2], (0, "screen\n"))

    def test_batch_failure_keeps_other_keys(self):
        self.host.create("w1", sid="replacement")
        self.host.create("w2", sid=SID)
        records = (
            self.record.to_line()
            + "\n"
            + replace(self.record, name="w2", sessions=("w2",)).to_line()
            + "\n"
        )
        code, out, err = self.invoke("attach-command", stdin=records)
        self.assertEqual(code, 1)
        self.assertIn(SID, err)
        self.assertEqual(out, "attach-session\n")
