"""How a test's child Python imports agent_pm: from this tree's import root (ROOT), never another checkout's."""

import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "core", "src")
PY = (sys.executable, "-P", "-s")


def child_env(base=None, *extra: str) -> dict[str, str]:
    env = os.environ if base is None else base
    return {
        **{k: v for k, v in env.items() if not k.startswith("PYTHON")},
        "PYTHONPATH": os.pathsep.join((ROOT, *extra)),
    }
