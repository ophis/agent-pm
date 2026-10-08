"""Imported first by every test module (config reads the config on import), so no test reads this machine's local
config: points HOME at a temp dir of this process's own (removed at exit) before config's import, never this machine's
~/.agent-pm, which may hold this checkout; then loads core/src/tests/hermetic.py and adds orchestrator.local.toml to
what it hides. A test needing a local file writes its own under home()."""
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile

HOME = tempfile.mkdtemp(prefix="agent-pm-tests-home-")
atexit.register(shutil.rmtree, HOME, ignore_errors=True)
os.environ["HOME"] = HOME
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
sys.path.insert(0, os.path.join(ROOT, "core", "src"))
_spec = importlib.util.spec_from_file_location("core_hermetic", os.path.join(ROOT, "core", "src", "tests", "hermetic.py"))
_core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_core)
FIXTURE, HIDDEN, home = _core.FIXTURE, _core.HIDDEN, _core.home
HIDDEN.add(os.path.realpath(os.path.expanduser("~/.agent-pm/orchestrator.local.toml")))
