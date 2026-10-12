"""Every module of agent_pm and new.tests imported alone in a fresh child, an empty temp dir its HOME and cwd: it
exits 0, leaving the dir empty and sys.path unchanged."""

from new.tests.case import NEW, PY, ROOT, Case, child_env

import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(NEW)
CHILD = """import importlib, sys
path = list(sys.path)
importlib.import_module(sys.argv[1])
sys.exit(sys.path != path and f"sys.path {path} became {sys.path}")
"""


def modules(root: str, package: str) -> list[str]:
    found = []
    for top, dirs, files in os.walk(os.path.join(root, *package.split("."))):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(f for f in files if f.endswith(".py")):
            parts = os.path.relpath(os.path.join(top, name), root)[:-3].split(os.sep)
            found.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return found


class ImportsAlone(Case):
    def test_each_module(self):
        names = modules(ROOT, "agent_pm") + modules(REPO, "new.tests")
        self.assertIn("agent_pm.driver.api", names)
        self.assertIn("new.tests.case", names)
        for name in names:
            with self.subTest(name), tempfile.TemporaryDirectory() as home:
                extra = (REPO,) if name.startswith("new.") else ()
                res = subprocess.run(
                    [*PY, "-c", CHILD, name],
                    cwd=home,
                    env={**child_env(None, *extra), "HOME": home},
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual((res.returncode, os.listdir(home)), (0, []), res.stderr)


if __name__ == "__main__":
    unittest.main()
