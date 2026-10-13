"""HookError failures raised by the bundled session runtime helpers."""

from __future__ import annotations


class HookError(Exception):
    """One stderr line; the hook stops."""

    @classmethod
    def unloadable_host(cls, path) -> HookError:
        return cls(f"tmux hook helper {path}: cannot load Python module")

    @classmethod
    def usage(cls) -> HookError:
        return cls("usage: tui_hook.py <absolute session dir> <label [A-Za-z0-9_.-]{1,64}>")

    @classmethod
    def oversized_payload(cls, limit: int) -> HookError:
        return cls(f"payload: over {limit} bytes")

    @classmethod
    def non_regular(cls, path: str) -> HookError:
        return cls(f"{path}: not a regular file")

    @classmethod
    def file_access(cls, path: str, cause: OSError) -> HookError:
        return cls(f"{path}: {cause.strerror}")

    @classmethod
    def oversized_state(cls, path: str, limit: int) -> HookError:
        return cls(f"{path}: over {limit} bytes")

    @classmethod
    def non_object_state(cls, path: str) -> HookError:
        return cls(f"{path}: not a JSON object")

    @classmethod
    def invalid_state_field(cls, path: str, field: str) -> HookError:
        return cls(f"{path}: bad {field}")

    @classmethod
    def adapter_access(cls, path: str, cause: OSError) -> HookError:
        return cls(f"adapter hook {path}: {cause.strerror}")

    @classmethod
    def unsafe_adapter(cls, path: str) -> HookError:
        return cls(f"adapter hook {path}: not a regular file owned by you or root")

    @classmethod
    def unloadable_adapter(cls, path: str) -> HookError:
        return cls(f"adapter hook {path}: cannot load Python module")

    @classmethod
    def missing_callback(cls, path: str) -> HookError:
        return cls(f"adapter hook {path}: missing on_hook callback")

    @classmethod
    def unavailable_tmux(cls, path: str, cause: OSError) -> HookError:
        return cls(f"tmux {path}: {cause.strerror}")

    @classmethod
    def unsafe_tmux(cls, path: str) -> HookError:
        return cls(f"tmux {path}: not a regular executable file owned by you or root")

    @classmethod
    def short_write(cls, path: str) -> HookError:
        return cls(f"{path}: short write")
