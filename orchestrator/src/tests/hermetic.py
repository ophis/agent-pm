"""Imported first by every test module: hides the repo's real core/ and orchestrator/config.local.toml (os.path.isfile,
which repo.read_config asks, says they are absent, through symlinks too), from config's import on, so tests read only
the committed configs and their fixtures."""
import os
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
HIDDEN = {os.path.realpath(os.path.join(ROOT, d, "config.local.toml")) for d in ("core", "orchestrator")}
_isfile = os.path.isfile
mock.patch("os.path.isfile", lambda path: _isfile(path) and os.path.realpath(path) not in HIDDEN).start()
