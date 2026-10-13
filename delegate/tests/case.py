"""Case, every delegate test's base; the committed [adapters] (committed); child helpers (PY, child_env); this
tree's import roots (REPO, SRC, ROOTS, PYTHONPATH)."""

import os
import sys
import tempfile
import unittest
from unittest import mock

import agent_pm
from agent_pm.session import Session
from agent_pm.session.adapters import registry
from agent_pm.utils.config_file import read_project
from agent_pm.utils.paths import import_roots

from session.tests.case import settings

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOTS = import_roots(REPO)
SRC = {os.path.basename(os.path.dirname(r)).replace("-", "_"): r for r in ROOTS}
PYTHONPATH = os.pathsep.join(ROOTS)
RUN_WITH = os.pathsep.join(os.path.relpath(root, REPO) for root in ROOTS)
PY = (sys.executable, "-P", "-s")
_settings = registry.settings


def committed(local: str = os.devnull) -> dict:
    """The session config's [adapters], `local` merged (default none), even under Case.configure_session."""
    return _settings(read_project("session", local=local))["adapters"]


class Case(unittest.TestCase):
    """Fails a test whose agent_pm is not exactly this tree's, else runs it (whatever its setUp)
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

    def configure_session(self, adapters, tmux=None) -> None:
        """Session.configure with settings(adapters, tmux), also what Session.settings returns for the rest
        of the test, so an entry configuring again keeps them."""
        given = settings(adapters, tmux)
        self.enterContext(mock.patch.object(registry, "settings", return_value=given))
        Session.configure(**given)


def _foreign() -> str | None:
    own = {os.path.realpath(os.path.join(r, "agent_pm")) for r in ROOTS}
    for path in agent_pm.__path__:
        if os.path.realpath(path) not in own:
            return f"agent_pm.__path__ holds {path}, not only this tree's: run with PYTHONPATH={RUN_WITH}"
    if missing := own - {os.path.realpath(p) for p in agent_pm.__path__}:
        return (
            f"agent_pm.__path__ lacks {', '.join(sorted(missing))}: run with PYTHONPATH={RUN_WITH}"
        )
    for name, module in list(sys.modules.items()):
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


KEEP = ("PYTHONDONTWRITEBYTECODE",)


def child_env(base=None, *extra: str) -> dict[str, str]:
    env = os.environ if base is None else base
    return {
        **{k: v for k, v in env.items() if not k.startswith("PYTHON") or k in KEEP},
        "PYTHONPATH": os.pathsep.join((*ROOTS, *extra)),
    }
