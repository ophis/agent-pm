"""Path loader for standalone Codex scripts and their shared argv scanner."""

import importlib.util
import os
import sys

DIRECTORY = os.path.dirname(os.path.abspath(__file__))


def load(name):
    directory = os.path.dirname(DIRECTORY) if name == "flags" else DIRECTORY
    key, path = "_agent_pm_codex_" + name, os.path.join(directory, name + ".py")
    if key in sys.modules:
        found = getattr(sys.modules[key], "__file__", None)
        if found != path:
            raise ImportError(f"{key} is {found}, not {path}")
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path}: cannot load Python module")
    # Postponed dataclass annotations resolve through their registered module.
    module = sys.modules[key] = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[key]
        raise
    return module
