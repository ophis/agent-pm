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


def load_siblings(directory: str, *dependencies: tuple[str, str]) -> tuple[ModuleType, ...]:
    return tuple(
        load_file(name, os.path.join(directory, relative)) for name, relative in dependencies
    )


def load_controls(directory: str) -> tuple[ModuleType, ...]:
    return load_siblings(
        directory,
        ("agent_pm.session.hosts.tmux.commands", "commands.py"),
        ("agent_pm.session.hosts.tmux.errors.types", "errors/types.py"),
        ("agent_pm.session.hosts.tmux.model.types", "model/types.py"),
    )
