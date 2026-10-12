"""Before any test module loads, HOME becomes a temp dir of this process's own, never this machine's ~/.agent-pm."""

import atexit
import os
import shutil
import tempfile
from unittest import mock

HOME = tempfile.mkdtemp(prefix="agent-pm-new-tests-home-")
atexit.register(shutil.rmtree, HOME, ignore_errors=True)
os.environ["HOME"] = HOME


def home(case) -> str:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    patch = mock.patch.dict(os.environ, {"HOME": tmp.name})
    patch.start()
    case.addCleanup(patch.stop)
    path = os.path.join(tmp.name, ".agent-pm")
    os.mkdir(path)
    return path
