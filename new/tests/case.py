"""Case, every test's base, and the child helpers: agent_pm only from this tree's import root (ROOT)."""

import os
import sys
import tempfile
import unittest
from unittest import mock

from agent_pm.driver import registry
from agent_pm.utils import paths

NEW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(NEW, "core")
ROOT = os.path.join(CORE, "src")
PY = (sys.executable, "-P", "-s")


class Case(unittest.TestCase):
    """Fails a test at once if agent_pm is another tree's, else runs it (whatever its setUp) in a fresh temp HOME,
    self.home (self.agent_pm: its .agent-pm), the registry reset before and after."""

    def run(self, result=None):
        if os.path.realpath(paths.IMPORT_ROOT) != os.path.realpath(ROOT):
            result = self.defaultTestResult() if result is None else result
            result.startTest(self)
            try:
                self.fail(
                    f"agent_pm is imported from {paths.IMPORT_ROOT}, not this tree's {ROOT}: "
                    "run with PYTHONPATH=new/core/src"
                )
            except self.failureException:
                result.addFailure(self, sys.exc_info())
            result.stopTest(self)
            return result
        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as self.home,
            mock.patch.dict(os.environ, {"HOME": self.home}),
        ):
            self.agent_pm = os.path.join(self.home, ".agent-pm")
            os.mkdir(self.agent_pm)
            registry.reset()
            try:
                return super().run(result)
            finally:
                registry.reset()


def child_env(base=None, *extra: str) -> dict[str, str]:
    env = os.environ if base is None else base
    return {
        **{k: v for k, v in env.items() if not k.startswith("PYTHON")},
        "PYTHONPATH": os.pathsep.join((ROOT, *extra)),
    }
