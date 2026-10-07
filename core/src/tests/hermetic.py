"""Imported first by the tests that read core's own config: hides this machine's repo.LOCAL (os.path.isfile, which
repo.read_config asks, says it is absent, through symlinks too) and merges core.local.fixture.toml over the committed
core/config.toml in its place, so they read only the committed config and fixtures. A test writes its own local files
under home()."""
import os
import tempfile
import tomllib
from unittest import mock

import repo

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.realpath(os.path.join(HERE, "..", ".."))
HIDDEN = {os.path.realpath(os.path.expanduser(repo.LOCAL))}
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
