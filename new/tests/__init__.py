"""The new core's tests, run with PYTHONPATH=new/core/src; hermetic.py runs before any test module loads."""

import os

NEW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(NEW, "core")

from . import hermetic  # noqa: E402,F401
