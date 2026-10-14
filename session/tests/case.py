"""Case, every session test's base; session settings (HOSTS, configured); child helpers
(PY, child_env); this tree's import roots (REPO, SRC, ROOTS), PLUGIN_BIN (first on PATH); SPAWNS, the
calls that start a process.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

import agent_pm
from agent_pm.session import Host
from agent_pm.session.engine.hosted_session import HostedSession
from agent_pm.session.hosts.tmux import TmuxHost

from utils.tests.sandbox import clean_env, project_roots

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUNDLE = os.environ.get("AGENT_PM_SESSION_BUNDLE")
ROOTS = (BUNDLE,) if BUNDLE else project_roots(REPO)
SRC = (
    dict.fromkeys(("session", "utils"), BUNDLE)
    if BUNDLE
    else {os.path.basename(os.path.dirname(r)).replace("-", "_"): r for r in ROOTS}
)
RUN_WITH = os.pathsep.join(os.path.relpath(root, REPO) for root in ROOTS)
PLUGIN_BIN = os.path.join(REPO, "bin")
PY = (sys.executable, "-P", "-S")
SPAWNS = ("subprocess.Popen", "os.fork", "os.posix_spawn", "os.posix_spawnp")
HOSTS = {"tmux": {"implementation": "agent_pm.session.hosts.tmux:TmuxHost"}}


def configured(adapters, tmux=None, *, host=None, **dependencies) -> type[HostedSession]:
    """A HostedSession class of `adapters`, HOSTS with `tmux` in [hosts.tmux]; each TmuxHost holds
    Tmux `host` (None: its own); dependencies: class_loader, probe, clock.
    """
    hosts = Host.parse_hosts({"tmux": {**HOSTS["tmux"], **(tmux or {})}, "default": "tmux"})

    def build(kind: type[Host], name: str, table) -> Host:
        return kind(name, table, tmux=host) if issubclass(kind, TmuxHost) else kind(name, table)

    return HostedSession.configured(
        adapters=adapters, hosts=hosts, host_factory=build, **dependencies
    )


class Case(unittest.TestCase):
    """Fails a test whose agent_pm is not exactly this tree's, else runs it (whatever its setUp) in
    a fresh temp HOME, self.home (self.agent_pm: its .agent-pm), a fresh session class
    (self.session_cls) configured with self.session_dependencies.
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
            mock.patch.dict(
                os.environ,
                {
                    **clean_env(None, *ROOTS),
                    "HOME": self.home,
                    "TUI_ATTACH_PREFIX": "",
                    "PATH": os.pathsep.join((PLUGIN_BIN, os.environ.get("PATH", os.defpath))),
                },
                clear=True,
            ),
        ):
            self.agent_pm = os.path.join(self.home, ".agent-pm")
            os.mkdir(self.agent_pm)
            self.configure_session({})
            return super().run(result)

    def configure_session(self, adapters, tmux=None) -> None:
        self.session_cls = configured(adapters, tmux, **getattr(self, "session_dependencies", {}))


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
