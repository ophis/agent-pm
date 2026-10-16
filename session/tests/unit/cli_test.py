import contextlib
import io

from agent_pm.session import cli
from agent_pm.session.engine.hosted_session import HostedSession

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
