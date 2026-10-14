"""Real anchored groups retain descendants and retire their control generation."""

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent_pm.utils import ProcessControl

from utils.tests.case import Case


class ProcessControlTest(Case):
    def start(self, script, *args):
        control = ProcessControl.prepare([sys.executable, "-I", "-c", script, *args], os.getcwd())
        process = subprocess.Popen(
            control.command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def cleanup():
            with contextlib.suppress(OSError):
                control.kill()
            process.communicate(timeout=10)
            control.remove()

        self.addCleanup(cleanup)
        control.wait_started()
        return control, process

    def test_natural_exit_retires_the_generation_without_an_earlier_poll(self):
        control, process = self.start("print('exact output'); raise SystemExit(3)")
        self.assertEqual(process.communicate(timeout=10), (b"exact output\n", b""))
        self.assertEqual(process.returncode, 3)
        control.kill()
        self.assertFalse(control.live())

    def test_the_controller_keeps_ownership_after_the_program_leaves_a_descendant(self):
        with tempfile.TemporaryDirectory() as folder:
            pids = Path(folder, "child")
            control, process = self.start(
                "import os, sys, time; child = os.fork(); "
                "open(sys.argv[1], 'w').write(str(child)) if child else time.sleep(60)",
                str(pids),
            )
            deadline = time.monotonic() + 10
            while not pids.exists() or not pids.read_text():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            child = int(pids.read_text())
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            self.assertTrue(control.live())
            os.kill(child, 0)
            control.kill()
            process.communicate(timeout=10)
            self.assertFalse(control.live())
