"""Case, every test's base, and the child helpers: namespace package agent_pm only from this tree's import
roots (ROOT, ORCH_ROOT)."""

import os
import sys
import tempfile
import unittest
from unittest import mock

import agent_pm
from agent_pm.session.adapters import registry

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(REPO, "core")
ROOT = os.path.join(CORE, "src")
ORCH_ROOT = os.path.join(REPO, "linear-orchestrator", "src")
PY = (sys.executable, "-P", "-s")
PYTHONPATH = "core/src:linear-orchestrator/src"


class Case(unittest.TestCase):
    """Fails a test at once if any part of agent_pm is another tree's, else runs it (whatever its setUp)
    in a fresh temp HOME, self.home (self.agent_pm: its .agent-pm), the registry reset before and after."""

    def run(self, result=None):
        if foreign := _foreign():
            result = self.defaultTestResult() if result is None else result
            result.startTest(self)
            try:
                self.fail(foreign)
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


def _foreign() -> str | None:
    own = {os.path.realpath(os.path.join(r, "agent_pm")) for r in (ROOT, ORCH_ROOT)}
    for path in agent_pm.__path__:
        if os.path.realpath(path) not in own:
            return f"agent_pm.__path__ holds {path}, not only this tree's: run with PYTHONPATH={PYTHONPATH}"
    for name, module in list(sys.modules.items()):
        top, _, sub = name.partition(".")
        where = getattr(module, "__file__", None)
        if top == "agent_pm" and sub and "." not in sub and where:
            found = os.path.dirname(os.path.dirname(where))
            found = os.path.dirname(found) if os.path.basename(where) == "__init__.py" else found
            root = ORCH_ROOT if sub == "linear_orchestrator" else ROOT
            if os.path.realpath(found) != os.path.realpath(root):
                return (
                    f"{name} is imported from {found}, not this tree's {root}: "
                    f"run with PYTHONPATH={PYTHONPATH}"
                )
    return None


def child_env(base=None, *extra: str) -> dict[str, str]:
    env = os.environ if base is None else base
    return {
        **{k: v for k, v in env.items() if not k.startswith("PYTHON")},
        "PYTHONPATH": os.pathsep.join((ROOT, *extra)),
    }
