"""Imported first by every test module: points HOME at a temp dir of this process's own (removed at exit) before config's
import, so ~ and config.WORK_DIR resolve there, never in this machine's ~/.agent-pm, which may hold this checkout; hides
~/.agent-pm/core.local.toml and orchestrator.local.toml (os.path.isfile, which repo.read_config asks, says they are
absent, through symlinks too), and merges core's test fixture over the committed core/config.toml
(core/src/tests/hermetic.py), so tests read only the committed configs and their fixtures. A test writes its own local
files under home()."""
import atexit
import importlib.util
import os
import shutil
import sys
import tempfile

HOME = tempfile.mkdtemp(prefix="agent-pm-tests-home-")
atexit.register(shutil.rmtree, HOME)
os.environ["HOME"] = HOME
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
sys.path.insert(0, os.path.join(ROOT, "core", "src"))
_spec = importlib.util.spec_from_file_location("core_hermetic", os.path.join(ROOT, "core", "src", "tests", "hermetic.py"))
_core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_core)
FIXTURE, HIDDEN, home = _core.FIXTURE, _core.HIDDEN, _core.home
HIDDEN.add(os.path.realpath(os.path.expanduser("~/.agent-pm/orchestrator.local.toml")))
