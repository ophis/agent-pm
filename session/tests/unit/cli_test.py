import contextlib
import io
from unittest import mock

from agent_pm.session import Hosts, Session, cli
from agent_pm.session.engine.hosted_session import HostedSession
from agent_pm.utils import Record

from session.tests.case import Case


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


class Content(Case):
    def test_key_and_stdin_record_print_the_sessions_content(self):
        record = Record(
            1,
            "worker",
            "w1",
            ("w1",),
            "sid",
            "/w",
            ("agent-pm-session",),
            ("agent-pm-session", "resume", "w1"),
            None,
            "test",
        )
        for arguments, stdin, sid in (
            (["content", "w1"], "", None),
            (["content"], record.to_line() + "\n", "sid"),
        ):
            with self.subTest(arguments=arguments):
                code, out, err, attach = self.content(arguments, stdin)
                self.assertEqual((code, out, err), (0, "screen\nsecond line\n", ""))
                attach.assert_called_once_with("w1", sid=sid, host="fake")

    def test_invalid_or_missing_or_multiple_keys_answer_nothing(self):
        a = Record(
            1,
            "worker",
            "a",
            ("a",),
            "sid",
            "/w",
            ("agent-pm-session",),
            ("agent-pm-session", "resume", "w1"),
            None,
            "test",
        )
        b = Record(
            1,
            "worker",
            "b",
            ("b",),
            "sid2",
            "/w",
            ("agent-pm-session",),
            ("agent-pm-session", "resume", "w1"),
            None,
            "test",
        )
        for arguments, stdin in (
            (["content"], ""),
            (["content", "a", "b"], ""),
            (["content"], a.to_line() + "\n" + b.to_line() + "\n"),
            (["content"], "not a Record\n"),
            (["content"], a.to_line() + "\nnot a Record\n"),
        ):
            with self.subTest(arguments=arguments, stdin=stdin):
                code, out, err, attach = self.content(arguments, stdin)
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err)
                attach.assert_not_called()

    def content(self, arguments, stdin):
        out, err = io.StringIO(), io.StringIO()
        view = mock.Mock()
        view.content.return_value = "screen\nsecond line"
        with (
            mock.patch.object(cli, "_configure", return_value=Hosts("fake", {})),
            mock.patch.object(cli, "_host_of", return_value="fake"),
            mock.patch.object(Session, "attach", return_value=view) as attach,
            mock.patch("sys.stdin", io.StringIO(stdin)),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = cli.main(arguments)
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue(), attach
