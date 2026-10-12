"""The new tests: SRC goes first on sys.path, ORCH_SRC right after it, then hermetic.py runs, before any test loads."""
import os
import sys

NEW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(NEW, "core")
SRC = os.path.join(CORE, "src")
ORCHESTRATOR = os.path.join(NEW, "orchestrator")
ORCH_SRC = os.path.join(ORCHESTRATOR, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)
if ORCH_SRC not in sys.path:
    sys.path.insert(sys.path.index(SRC) + 1, ORCH_SRC)

from . import hermetic  # noqa: E402,F401
