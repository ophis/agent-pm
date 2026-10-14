"""Case, every delegate test's base; the committed [adapters] (committed); child helpers (PY,
child_env); this tree's import roots (REPO, SRC, ROOTS, PYTHONPATH).
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

import agent_pm
from agent_pm.delegate.engine.drivers import AgentDriver
from agent_pm.session import Driver
from agent_pm.utils import read_project

from session.tests.case import settings
from utils.tests.sandbox import clean_env, project_roots

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOTS = project_roots(REPO)
SRC = {os.path.basename(os.path.dirname(r)).replace("-", "_"): r for r in ROOTS}
PYTHONPATH = os.pathsep.join(ROOTS)
RUN_WITH = os.pathsep.join(os.path.relpath(root, REPO) for root in ROOTS)
PY = (sys.executable, "-P", "-S")


def committed(local: str = os.devnull) -> dict:
    """The session config's [adapters], `local` merged (default none), even under
    Case.configure_session.
    """
    return read_project("session", local=local)["adapters"]


class Case(unittest.TestCase):
    """Fails a test whose agent_pm is not exactly this tree's, else runs it (whatever its setUp) in
    a fresh temp HOME, self.home (self.agent_pm: its .agent-pm), fresh caller-owned session
    settings.
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
            self.configure_session({})
            return super().run(result)

    def configure_session(self, adapters, tmux=None) -> None:
        self.driver = FakeDriver(
            Driver(**settings(adapters, tmux), **getattr(self, "driver_dependencies", {}))
        )


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


class FakeDriver:
    """A forwarding fake whose per-test methods may be replaced."""

    session_based = True
    session = None
    display = None

    def __init__(self, driver):
        self.driver = driver

    def policy(self, client):
        return AgentDriver(self.driver.session_cls).policy(client)

    def start(self, spec, *, display=None, host=None, idle_timeout=None):
        self.session = AgentDriver(self.driver.session_cls).start(
            spec, display=display, host=host, idle_timeout=idle_timeout
        )
        return self.session

    def writable_dirs(self, spec):
        return self.driver.session_cls.writable_dirs(spec)

    def preflight(self, spec):
        AgentDriver(self.driver.session_cls).preflight(spec)

    def __getattr__(self, name):
        return getattr(self.driver, name)
