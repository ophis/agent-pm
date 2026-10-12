"""The new core's tests: SRC goes first on sys.path, then hermetic.py runs, before any test module loads."""
import os
import sys

NEW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(NEW, "core")
SRC = os.path.join(CORE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from . import hermetic  # noqa: E402,F401
