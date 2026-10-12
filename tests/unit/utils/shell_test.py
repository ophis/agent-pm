"""core/src/agent_pm/utils/repos/shell.py: err_text and redact."""

import subprocess
import unittest

from agent_pm.utils.repos import shell

from tests.case import Case


class ErrText(Case):
    def test_userinfo_is_redacted(self):
        res = subprocess.CompletedProcess(
            [],
            1,
            "",
            "fatal: could not read Password for 'https://SECRETTOK@github.com': no\n"
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
                self.assertEqual(
                    shell.err_text(subprocess.CompletedProcess([], 1, stderr=stderr)), want
                )


if __name__ == "__main__":
    unittest.main()
