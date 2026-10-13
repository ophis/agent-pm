"""Load Codex standalone dependencies by absolute path, registering only owned modules."""

from __future__ import annotations

import importlib.util
import os
import sys

DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LoadingError(ImportError):
    @classmethod
    def collision(cls, key, found, path) -> LoadingError:
        return cls(f"{key} is {found}, not {path}")

    @classmethod
    def unavailable(cls, path) -> LoadingError:
        return cls(f"cannot load Python module {path}")


def sibling(name):
    key, path = "_agent_pm_codex_" + name, os.path.join(DIRECTORY, name + ".py")
    if key in sys.modules:
        found = getattr(sys.modules[key], "__file__", None)
        if found != path:
            raise LoadingError.collision(key, found, path)
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise LoadingError.unavailable(path)
    # Dataclasses with postponed annotations resolve their registered module.
    module = sys.modules[key] = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[key]
        raise
    return module
