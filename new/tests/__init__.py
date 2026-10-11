"""The new core's tests. NEW, CORE and SRC are the absolute paths of new/, new/core and new/core/src; SRC goes first on
sys.path, then hermetic.py runs, so every test process has a temp HOME before any test module loads. Nothing here
imports or adds the repo's tests/ or its old core."""
import os
import sys

NEW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(NEW, "core")
SRC = os.path.join(CORE, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from . import hermetic  # noqa: E402,F401
