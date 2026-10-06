"""Imported first by the tests that read core's own config: hides a real core/config.local.toml (os.path.isfile, which
repo.read_config asks, says it is absent, through symlinks too), so they read only the committed config and fixtures."""
import os
from unittest import mock

HIDDEN = {os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config.local.toml"))}
_isfile = os.path.isfile
mock.patch("os.path.isfile", lambda path: _isfile(path) and os.path.realpath(path) not in HIDDEN).start()
