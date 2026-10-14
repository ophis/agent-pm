"""Case, every linear-orchestrator test's base; child helpers (PY, child_env, ORCH_ROOT); this
tree's import roots (REPO, SRC, ROOTS, PYTHONPATH).
"""

import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

import agent_pm
from agent_pm.linear_orchestrator.engine import log
from agent_pm.utils import import_roots

from utils.tests.sandbox import clean_env

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ROOTS = import_roots(REPO)
SRC = {os.path.basename(os.path.dirname(r)).replace("-", "_"): r for r in ROOTS}
PYTHONPATH = os.pathsep.join(ROOTS)
RUN_WITH = os.pathsep.join(os.path.relpath(root, REPO) for root in ROOTS)
PY = (sys.executable, "-P", "-s")
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
            mock.patch.dict(os.environ, {**clean_env(), "HOME": self.home}, clear=True),
            ExitStack() as stack,
        ):
            if ".unit." in self.id():
                stack.enter_context(mock.patch("subprocess.run", _getconf))
            log.use_logs(None)
            self.agent_pm = os.path.join(self.home, ".agent-pm")
            os.mkdir(self.agent_pm)
            return super().run(result)


def _foreign(modules=None) -> str | None:
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
                return (
                    f"{name} is imported from {found}, not this tree's {root}: "
                    f"run with PYTHONPATH={RUN_WITH}"
                )
    return None


def child_env(base=None, *extra: str) -> dict[str, str]:
    return clean_env(base, *ROOTS, *extra)


def _getconf(argv, *_args, **_kwargs):
    if argv == ["getconf", "DARWIN_USER_TEMP_DIR"]:
        return SimpleNamespace(stdout="")
    if "rev-parse" in argv and "--show-toplevel" in argv:
        cwd = argv[argv.index("-C") + 1] if "-C" in argv else _kwargs.get("cwd", REPO)
        inside = os.path.commonpath((os.path.realpath(cwd), REPO)) == REPO
        return subprocess.CompletedProcess(
            argv,
            0 if inside else 128,
            stdout="true\n" + REPO + "\n" if inside else "",
            stderr=""
            if inside
            else "fatal: not a git repository (or any of the parent directories): .git",
        )
    raise AssertionError(f"unexpected subprocess: {argv}")
