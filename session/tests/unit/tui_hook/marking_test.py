"""Marking behaviour."""

import os
import subprocess
import sys

from session.tests.unit.tui_hook.case import HOOK, HookBase


class MarkingTest(HookBase):
    def test_a_failed_mark_is_one_stderr_line_after_the_record(self):
        self.assertError(self.hook("stop", TMUX_FAIL="1"), "tmux: boom again")
        os.unlink(self.events)
        self.assertError(self.hook("ask"), "events file")
        self.assertFalse(os.path.exists(self.events))
        self.assertEqual([r["label"] for r in self.records()], ["stop", "ask"])

    def test_disabled_logging_reports_exactly_one_error_line_and_exits_zero(self):
        script = """import logging, runpy, sys
logging.disable(logging.CRITICAL)
path = sys.argv[1]
sys.argv = [path]
runpy.run_path(path, run_name="__main__")
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, HOOK],
            input=b"{}",
            capture_output=True,
            env=self.env,
            check=True,
        )
        self.assertEqual(result.stdout, b"")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertTrue(result.stderr.startswith(b"tui_hook: usage: tui_hook.py "))

    def test_an_unexpected_error_still_exits_0(self):
        res = subprocess.run(
            [sys.executable, "-I", HOOK, self.dir, "stop"],
            capture_output=True,
            env=self.env,
            timeout=60,
            preexec_fn=lambda: os.close(0),
        )
        self.assertEqual((res.returncode, res.stdout), (0, b""), res.stderr)
        self.assertError(res.stderr.decode(), "Error")
        self.assertNothingWritten()
