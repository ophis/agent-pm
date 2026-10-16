import contextlib
import io

from agent_pm.session import cli

from session.tests.case import Case


class ReservedName(Case):
    def test_start_none_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            cli._parser(None).parse_args(["start", "none", "--client", "claude"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("argument name: want [A-Za-z0-9_-]+ but not none, not 'none'", err.getvalue())
