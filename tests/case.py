"""Case, the repo-wide checks' base; child helpers (PY, child_env, ORCH_ROOT); this tree's import
roots (REPO, SRC, ROOTS).
"""

import glob
import os
import sys
import tempfile
import unittest
from unittest import mock

from utils.tests.sandbox import clean_env

try:
    import agent_pm
    from agent_pm.utils.paths import import_roots
except ImportError:
    agent_pm = None

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOTS = import_roots(REPO) if agent_pm else tuple(sorted(glob.glob(os.path.join(REPO, "*", "src"))))
SRC = {os.path.basename(os.path.dirname(r)).replace("-", "_"): r for r in ROOTS}
RUN_WITH = os.pathsep.join(os.path.relpath(root, REPO) for root in ROOTS)
PY = (sys.executable, "-P", "-S")
ORCH_ROOT = os.path.join(REPO, "linear-orchestrator", "src")


class Case(unittest.TestCase):
    """Fails a test whose agent_pm is not exactly this tree's, else runs it (whatever its setUp) in
    a fresh temp HOME, self.home (self.agent_pm: its .agent-pm).
    """

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
            return super().run(result)


def _missing() -> str | None:
    return None if agent_pm else f"agent_pm is not importable: run with PYTHONPATH={RUN_WITH}"


def _foreign(modules=None) -> str | None:
    if missing := _missing():
        return missing
    own = {os.path.realpath(os.path.join(r, "agent_pm")) for r in ROOTS}
    for path in agent_pm.__path__:
        if os.path.realpath(path) not in own:
            return f"agent_pm.__path__ holds {path}, not only this tree's: run with PYTHONPATH={RUN_WITH}"
    if missing := own - {os.path.realpath(p) for p in agent_pm.__path__}:
        return (
            f"agent_pm.__path__ lacks {', '.join(sorted(missing))}: run with PYTHONPATH={RUN_WITH}"
        )
    for name, module in list((sys.modules if modules is None else modules).items()):
        top, _, sub = name.partition(".")
        where = getattr(module, "__file__", None)
        if top == "agent_pm" and sub and "." not in sub and where:
            found = os.path.dirname(os.path.dirname(where))
            found = os.path.dirname(found) if os.path.basename(where) == "__init__.py" else found
            root = SRC.get(sub)
            if root is None or os.path.realpath(found) != os.path.realpath(root):
                return f"{name} is imported from {found}, not this tree's {root}: run with PYTHONPATH={RUN_WITH}"
    return None


def child_env(base=None, *extra: str) -> dict[str, str]:
    return clean_env(base, *ROOTS, *extra)
