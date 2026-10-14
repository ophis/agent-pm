""".github/isolation.py: the gate keeps process controller directories inside the test sandbox."""

import re
import tempfile
import unittest
from pathlib import Path

import isolation


class ControllerPlacement(unittest.TestCase):
    def test_a_controller_directory_outside_the_sandbox_fails_the_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox, workspace, outside = (Path(tmp, name) for name in ("sb", "ws", "out"))
            gate = isolation.Gate(Path(tmp), sandbox, workspace)
            gate.current = "fixture.test"
            for path in (sandbox / "apm-group-a", workspace / "apm-group-b", outside / "other-c"):
                gate.audit("tempfile.mkdtemp", (str(path),))
            misplaced = str(outside / "apm-group-d")
            message = f"fixture.test: process controller outside test sandbox: {misplaced}"
            with self.assertRaisesRegex(RuntimeError, re.escape(message)):
                gate.audit("tempfile.mkdtemp", (misplaced,))
            self.assertEqual(len(gate.violations), 1)
