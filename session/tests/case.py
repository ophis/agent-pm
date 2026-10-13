"""Case, every session test's base; session settings (HOSTS, settings); child helpers (PY, child_env); this
tree's import roots (REPO, SRC, ROOTS)."""

import os
import sys
import tempfile
import unittest
from unittest import mock

import agent_pm
from agent_pm.session import Session
from agent_pm.session.adapters import registry
from agent_pm.utils.paths import import_roots

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOTS = import_roots(REPO)
SRC = {os.path.basename(os.path.dirname(r)).replace("-", "_"): r for r in ROOTS}
RUN_WITH = os.pathsep.join(os.path.relpath(root, REPO) for root in ROOTS)
PY = (sys.executable, "-P", "-s")
HOSTS = {"tmux": {"implementation": "agent_pm.session.hosts.tmux:TmuxHost"}}


def settings(adapters, tmux=None) -> dict:
    """Session.configure's arguments: `adapters`, HOSTS with `tmux` in [hosts.tmux], default tmux."""
    hosts = {"tmux": {**HOSTS["tmux"], **(tmux or {})}}
    return {"adapters": adapters, "hosts": hosts, "default_host": "tmux"}


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
        Session.configure(**settings(adapters, tmux))


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
