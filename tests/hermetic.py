"""Imported first (`from tests import hermetic`) by every test module that reads the repo's config (root CLAUDE.md ›
Roles, tasks, rules): points HOME at a temp dir of this process's own (removed at exit) before anything reads it, never
this machine's ~/.agent-pm, which may hold this checkout; hides repo.LOCAL and ~/.agent-pm/orchestrator.local.toml
(os.path.isfile, which repo.read_config asks, says they are absent, through symlinks too); merges
core.local.fixture.toml over the committed core/config.toml in place of the local file."""
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
HIDDEN = {os.path.realpath(os.path.expanduser(p)) for p in (repo.LOCAL, "~/.agent-pm/orchestrator.local.toml")}
FIXTURE = os.path.join(HERE, "core.local.fixture.toml")
_isfile, _read = os.path.isfile, repo.read_config


def read_config(path, *local):
    cfg = _read(path, *local)
    if os.path.realpath(path) == os.path.join(CORE, "config.toml"):
        with open(FIXTURE, "rb") as f:
            cfg = repo.merge(cfg, tomllib.load(f))
    return cfg


def home(case) -> str:
    """A HOME of its own for the rest of `case`'s test; returns its ~/.agent-pm."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    patch = mock.patch.dict(os.environ, {"HOME": tmp.name})
    patch.start()
    case.addCleanup(patch.stop)
    path = os.path.join(tmp.name, ".agent-pm")
    os.mkdir(path)
    return path


mock.patch("os.path.isfile", lambda path: _isfile(path) and os.path.realpath(path) not in HIDDEN).start()
mock.patch.object(repo, "read_config", read_config).start()
