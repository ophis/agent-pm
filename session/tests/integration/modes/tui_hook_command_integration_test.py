"""The tui hook through `agent-pm-session hook DIR LABEL` (D6, FR-32: the entry TASK-375 points the
clients' hook config at): the journal, mark, event-line, payload and failure cases the by-path
hook runs (tui_hook_support's *Cases), through bin/agent-pm-session; the command's own roots
replace the roots argument.
"""

import json
import os
import subprocess
import sys
import unittest

from session.tests.case import REPO
from session.tests.tui_hook_support import HookBase, MarkingCases, PayloadCases, RecordCases

COMMAND = os.path.join(REPO, "bin", "agent-pm-session")


class ThroughCommand(HookBase):
    def hook(self, label, payload=None, args=None, **env):
        """As HookBase.hook, through the command; `args` replaces DIR LABEL."""
        data = (
            payload
            if isinstance(payload, bytes)
            else json.dumps({} if payload is None else payload).encode()
        )
        environ = {k: v for k, v in {**self.env, **env}.items() if v is not None}
        environ["PATH"] = os.pathsep.join(
            (environ.get("PATH", ""), os.path.dirname(sys.executable), "/usr/bin", "/bin")
        )
        res = subprocess.run(
            [COMMAND, "hook", *([self.dir, label] if args is None else args)],
            input=data,
            capture_output=True,
            env=environ,
            timeout=60,
        )
        self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)
        return res.stderr.decode()


class UsageThroughCommand(ThroughCommand):
    def test_a_wrong_argument_count_is_one_usage_line_and_exits_0_writing_nothing(self):
        for args in ([], [self.dir], [self.dir, "stop", "x"]):
            with self.subTest(args=args):
                self.assertError(self.hook(None, args=args), "usage: agent-pm-session hook")
        self.assertNothingWritten()

    def test_a_closed_stdin_is_one_stderr_line_and_exits_0_writing_nothing(self):
        res = subprocess.run(
            [COMMAND, "hook", self.dir, "tool"],
            capture_output=True,
            env={**self.env, "PATH": f"{os.path.dirname(sys.executable)}:/usr/bin:/bin"},
            timeout=60,
            preexec_fn=lambda: os.close(0),
        )
        self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)
        self.assertError(res.stderr.decode(), "Error")
        self.assertNothingWritten()


class RecordsThroughCommand(RecordCases, ThroughCommand):
    pass


class PayloadThroughCommand(PayloadCases, ThroughCommand):
    pass


class MarkingThroughCommand(MarkingCases, ThroughCommand):
    pass


if __name__ == "__main__":
    unittest.main()
