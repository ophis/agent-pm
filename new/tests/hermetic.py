"""Imported by new/tests/__init__.py, so before any test module loads: points HOME at a temp dir of this process's own
(removed at exit), never this machine's ~/.agent-pm, which may hold this checkout."""
import atexit
import os
import shutil
import tempfile
from unittest import mock

HOME = tempfile.mkdtemp(prefix="agent-pm-new-tests-home-")
atexit.register(shutil.rmtree, HOME, ignore_errors=True)
os.environ["HOME"] = HOME


def home(case) -> str:
    """A HOME of its own for the rest of `case`'s test; returns its ~/.agent-pm (created)."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    patch = mock.patch.dict(os.environ, {"HOME": tmp.name})
    patch.start()
    case.addCleanup(patch.stop)
    path = os.path.join(tmp.name, ".agent-pm")
    os.mkdir(path)
    return path
