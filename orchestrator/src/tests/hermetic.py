"""Imported first by every test module: hides the repo's real core/ and orchestrator/config.local.toml (os.path.isfile,
which repo.read_config asks, says they are absent, through symlinks too), from config's import on, and merges core's
test fixture over the committed core/config.toml (core/src/tests/hermetic.py), so tests read only the committed
configs and their fixtures."""
import importlib.util
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
sys.path.insert(0, os.path.join(ROOT, "core", "src"))
_spec = importlib.util.spec_from_file_location("core_hermetic", os.path.join(ROOT, "core", "src", "tests", "hermetic.py"))
_core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_core)
FIXTURE = _core.FIXTURE
_core.HIDDEN.add(os.path.realpath(os.path.join(ROOT, "orchestrator", "config.local.toml")))
