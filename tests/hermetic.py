import atexit
import os
import shutil
import tempfile
import tomllib
from unittest import mock

HOME = tempfile.mkdtemp(prefix="agent-pm-tests-home-")
atexit.register(shutil.rmtree, HOME, ignore_errors=True)
os.environ["HOME"] = HOME
import repo  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.realpath(os.path.join(HERE, "..", "core"))
HIDDEN = {
    os.path.realpath(os.path.expanduser(p))
    for p in (repo.LOCAL, "~/.agent-pm/orchestrator.local.toml")
}
FIXTURE = os.path.join(HERE, "core.local.fixture.toml")
_isfile, _read = os.path.isfile, repo.read_config


def read_config(path, *local):
    cfg = _read(path, *local)
    if os.path.realpath(path) == os.path.join(CORE, "config.toml"):
        with open(FIXTURE, "rb") as f:
            cfg = repo.merge(cfg, tomllib.load(f))
    return cfg


def home(case) -> str:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    patch = mock.patch.dict(os.environ, {"HOME": tmp.name})
    patch.start()
    case.addCleanup(patch.stop)
    path = os.path.join(tmp.name, ".agent-pm")
    os.mkdir(path)
    return path


mock.patch(
    "os.path.isfile", lambda path: _isfile(path) and os.path.realpath(path) not in HIDDEN
).start()
mock.patch.object(repo, "read_config", read_config).start()
