"""Imported first by the tests that read core's own config: hides a real core/config.local.toml (os.path.isfile, which
repo.read_config asks, says it is absent, through symlinks too) and merges config.local.fixture.toml over the committed
core/config.toml in its place, so they read only the committed config and fixtures."""
import os
import tomllib
from unittest import mock

import repo

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.realpath(os.path.join(HERE, "..", ".."))
HIDDEN = {os.path.join(CORE, repo.LOCAL)}
FIXTURE = os.path.join(HERE, "config.local.fixture.toml")
_isfile, _read = os.path.isfile, repo.read_config


def read_config(path):
    cfg = _read(path)
    if os.path.realpath(path) == os.path.join(CORE, "config.toml"):
        with open(FIXTURE, "rb") as f:
            cfg = repo.merge(cfg, tomllib.load(f))
    return cfg


mock.patch("os.path.isfile", lambda path: _isfile(path) and os.path.realpath(path) not in HIDDEN).start()
mock.patch.object(repo, "read_config", read_config).start()
