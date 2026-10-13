"""HookError failures raised by the bundled session runtime helpers."""

from __future__ import annotations


class HookError(Exception):
    """One stderr line; the hook stops."""

    @classmethod
    def unloadable_host(cls, path) -> HookError:
        return cls(f"tmux hook helper {path}: cannot load Python module")
