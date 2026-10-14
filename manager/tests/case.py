"""Case, every manager test's base; child helpers (PY, child_env); this tree's import roots (REPO,
SRC, ROOTS, PYTHONPATH).
"""

import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

import agent_pm
from agent_pm.utils import Record, import_roots

from utils.tests.fakes import ctl

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOTS = import_roots(REPO)
SRC = {os.path.basename(os.path.dirname(r)).replace("-", "_"): r for r in ROOTS}
PYTHONPATH = os.pathsep.join(ROOTS)
RUN_WITH = os.pathsep.join(os.path.relpath(root, REPO) for root in ROOTS)
PY = (sys.executable, "-P", "-s")


class Case(unittest.TestCase):
    """Fails a test whose agent_pm is not exactly this tree's, else runs it (whatever its setUp) in
    a fresh temp HOME, self.home (self.agent_pm: its .agent-pm), no shared mutable session settings.
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

    def setup_ctl(self) -> str:
        self.ctl_dir = os.path.join(self.home, "ctl")
        self.bin_dir = os.path.join(self.home, "bin")
        os.mkdir(self.bin_dir)
        command = ctl.install(self.bin_dir, self.ctl_dir)
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "PATH": os.pathsep.join(
                        (self.bin_dir, os.path.dirname(sys.executable), os.defpath)
                    )
                },
            )
        )
        return command

    def fake_record(
        self, name: str, *, display: str | None = None, sid: str | None = None
    ) -> Record:
        return Record(
            1,
            "fake",
            name,
            (name,),
            sid or str(uuid.uuid4()),
            None,
            (ctl.COMMAND,),
            (ctl.COMMAND, "resume", name),
            display,
            None,
        )

    @contextlib.contextmanager
    def listed(self, records=()):
        real_run = subprocess.run

        def run(argv, **options):
            if tuple(argv) == ("agent-pm-session", "ls", "--here", "--records"):
                return subprocess.CompletedProcess(
                    argv, 0, "".join(record.to_line() + "\n" for record in records), ""
                )
            return real_run(argv, **options)

        with mock.patch("subprocess.run", side_effect=run):
            yield


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


KEEP = ("PYTHONDONTWRITEBYTECODE",)


def child_env(base=None, *extra: str) -> dict[str, str]:
    env = os.environ if base is None else base
    return {
        **{k: v for k, v in env.items() if not k.startswith("PYTHON") or k in KEEP},
        "PYTHONPATH": os.pathsep.join((*ROOTS, *extra)),
    }
