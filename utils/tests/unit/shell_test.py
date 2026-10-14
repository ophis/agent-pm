"""utils/src/agent_pm/utils/shell.py: err_text and redact."""

import unittest
from types import SimpleNamespace

from agent_pm.utils import resume_command, shell

from utils.tests.case import Case


class ErrText(Case):
    def test_userinfo_is_redacted(self):
        res = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="fatal: could not read Password for 'https://SECRETTOK@github.com': no\n"
            "fatal: unable to access 'https://u:SECRET2@ghe.io/o/n/'",
        )
        text = shell.err_text(res)
        self.assertNotIn("SECRET", text)
        self.assertIn("'https://***@github.com'", text)
        self.assertIn("'https://***@ghe.io/o/n/'", text)

    def test_trimmed_and_capped(self):
        for stderr, want in (
            (None, ""),
            ("", ""),
            ("  HTTP 404: Not Found \n", "HTTP 404: Not Found"),
            ("x" * 300, "x" * 200),
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(shell.err_text(SimpleNamespace(returncode=1, stderr=stderr)), want)


class ResumeCommand(Case):
    def test_plain_argv_without_environment(self):
        self.assertEqual(
            resume_command("/work", {}, ("codex", "resume", "id")), "cd /work && codex resume id"
        )

    def test_quotes_directory_environment_and_arguments(self):
        self.assertEqual(
            resume_command(
                "/work dir",
                {"LABEL": "a b", "EMPTY": ""},
                ("claude", "--resume", "a; echo bad", ""),
            ),
            "cd '/work dir' && LABEL='a b' EMPTY='' claude --resume 'a; echo bad' ''",
        )

    def test_non_shell_names_are_quoted_env_operands(self):
        self.assertEqual(
            resume_command("/w", {"A-B": "a b", "X;Y": "$(bad)", "-x": ""}, ("tool",)),
            "cd /w && env -- 'A-B=a b' 'X;Y=$(bad)' -x= tool",
        )


if __name__ == "__main__":
    unittest.main()
