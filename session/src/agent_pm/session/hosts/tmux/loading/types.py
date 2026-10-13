"""Load bundled standard-library helpers by path, retaining their canonical module identity."""

from __future__ import annotations

import importlib.util
import os
import sys
from types import ModuleType


class LoadingError(ImportError):
    @classmethod
    def collision(cls, key, found, path) -> LoadingError:
        return cls(f"{key} is {found}, not {path}")

    @classmethod
    def unavailable(cls, path) -> LoadingError:
        return cls(f"cannot load Python module {path}")


def load_file(name: str, path: str) -> ModuleType:
    path = os.path.abspath(path)
    if name in sys.modules:
        module = sys.modules[name]
        found = getattr(module, "__file__", None)
        if found != path:
            raise LoadingError.collision(name, found, path)
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise LoadingError.unavailable(path)
    module = sys.modules[name] = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module
