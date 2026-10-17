"""Retained commands can lazily load resources after their fixture installation disappears."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from agent_pm.utils import installation, kill_group

from utils.tests.case import Case


class InstallationIntegrationTest(Case):
    def test_supervised_child_keeps_modules_resources_and_exits_without_retained_copies(self):
        source = Path(self.home) / "installation A"
        package = source / "utils/src/agent_pm/utils"
        package.mkdir(parents=True)
        shutil.copy2(installation.__file__, package / "installation.py")
        (source / "utils/src/fixture_module.py").write_text("VALUE = 'A'\n")
        (package / "resource.txt").write_text("resource A")
        gate, ready, outcome = (Path(self.home) / name for name in ("gate", "ready", "outcome"))
        script = source / "utils/src/child.py"
        script.write_text(
            "import pathlib, sys, time\n"
            "ready, gate, outcome = map(pathlib.Path, sys.argv[1:])\n"
            "ready.touch()\n"
            "while not gate.exists(): time.sleep(0.01)\n"
            "import fixture_module\n"
            "resource = (pathlib.Path(__file__).parent / 'agent_pm/utils/resource.txt').read_text()\n"
            "outcome.write_text(fixture_module.VALUE + resource)\n"
        )
        with installation.Installation(str(source), root=str(Path(self.home) / "kept")) as kept:
            folder = Path(kept.root)
            command = kept.command(
                (sys.executable, "-P", str(script), str(ready), str(gate), str(outcome))
            )
            env = kept.environment({**os.environ, "PYTHONPATH": str(source / "utils/src")})
            child = subprocess.Popen(
                command,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                process_group=0,
            )
            self.addCleanup(kill_group, child)
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        shutil.rmtree(source)
        installation.sweep(str(folder.parent), now=time.time() + 120)
        self.assertTrue(folder.exists())
        gate.touch()
        out, err = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, (out, err))
        self.assertEqual(outcome.read_text(), "Aresource A")
        self.assertFalse(folder.exists())
