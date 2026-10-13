"""Codex recovery records and failures shared by bundled runtime helpers."""

from __future__ import annotations

from dataclasses import dataclass


class RecoveryError(Exception):
    @classmethod
    def identity(cls) -> RecoveryError:
        return cls("invalid recovery state identity")

    @classmethod
    def absolute_path(cls, label: str) -> RecoveryError:
        return cls(f"invalid Codex recovery {label}: expected an absolute path")

    @classmethod
    def snapshot_field(cls, field: str) -> RecoveryError:
        return cls(f"invalid Codex recovery snapshot {field}")

    @classmethod
    def unreadable_session_file(cls, path: str, cause: OSError) -> RecoveryError:
        return cls(f"cannot read Codex session file {path}: {cause}")

    @classmethod
    def unsafe_session_file(cls, path: str) -> RecoveryError:
        return cls(f"unsafe Codex session file: {path}")

    @classmethod
    def malformed_metadata(cls, path: str) -> RecoveryError:
        return cls(f"malformed Codex session metadata: {path}")

    @classmethod
    def mismatched_metadata(cls, path: str) -> RecoveryError:
        return cls(f"mismatched Codex session metadata: {path}")

    @classmethod
    def codex_native_id(cls) -> RecoveryError:
        return cls("invalid Codex recovery native ID")

    @classmethod
    def duplicate_session_files(cls, identifier: str) -> RecoveryError:
        return cls(f"duplicate Codex session files for native ID {identifier}")

    @classmethod
    def mismatched_native_metadata(cls, identifier: str, path: str) -> RecoveryError:
        return cls(f"mismatched Codex session metadata for native ID {identifier}: {path}")

    @classmethod
    def ungranted_path(cls, path: str) -> RecoveryError:
        return cls(f"invalid Codex recovery writable grants: no recorded root covers {path}")

    @classmethod
    def non_json_state(cls) -> RecoveryError:
        return cls("invalid recovery snapshot: expected JSON-safe state")

    @classmethod
    def non_object_state(cls) -> RecoveryError:
        return cls("invalid recovery snapshot: expected a JSON object")

    @classmethod
    def run_id(cls) -> RecoveryError:
        return cls("invalid recovery run ID")

    @classmethod
    def malformed_state(cls, path: str | None = None) -> RecoveryError:
        return cls(
            "malformed recovery state" if path is None else f"malformed recovery state: {path}"
        )

    @classmethod
    def native_id(cls) -> RecoveryError:
        return cls("invalid recovery native ID")

    @classmethod
    def writable_roots(cls) -> RecoveryError:
        return cls("invalid recovery writable roots")

    @classmethod
    def symlink(cls, path: str) -> RecoveryError:
        return cls(f"recovery state path has a symlink: {path}")

    @classmethod
    def unsafe_directory(cls, path: str) -> RecoveryError:
        return cls(f"unsafe recovery state directory mode or owner: {path}")

    @classmethod
    def noncanonical_store(cls) -> RecoveryError:
        return cls("invalid Codex recovery store: expected a canonical path")

    @classmethod
    def writable_store(cls, root: str, path: str) -> RecoveryError:
        return cls(f"recovery state directory {root} is inside writable root {path}")

    @classmethod
    def unreadable_state(cls, path: str, cause: OSError) -> RecoveryError:
        return cls(f"cannot read recovery state {path}: {cause}")

    @classmethod
    def unsafe_state_file(cls, path: str) -> RecoveryError:
        return cls(f"unsafe recovery state file mode or owner: {path}")

    @classmethod
    def oversized_state_file(cls, path: str) -> RecoveryError:
        return cls(f"malformed recovery state: file too large: {path}")

    @classmethod
    def lock_timeout(cls) -> RecoveryError:
        return cls("Codex recovery state lock timed out")

    @classmethod
    def oversized_state(cls) -> RecoveryError:
        return cls("recovery state is too large")

    @classmethod
    def existing_state(cls, sid: str) -> RecoveryError:
        return cls(f"recovery state already exists for run ID {sid!r}")

    @classmethod
    def missing_state(cls, sid: str | None = None) -> RecoveryError:
        return cls(
            "missing Codex recovery state"
            if sid is None
            else f"missing recovery state for run ID {sid!r}"
        )

    @classmethod
    def already_bound(cls, sid: str) -> RecoveryError:
        return cls(f"recovery run ID {sid!r} is already bound to a different native ID")

    @classmethod
    def writable_dependency(cls, root: str, path: str) -> RecoveryError:
        return cls(f"Codex tui writable root {root} could rewrite hook dependency {path}")


@dataclass(frozen=True)
class SessionFile:
    path: str
    mtime: float
    metadata: dict


@dataclass(frozen=True)
class SessionState:
    workdir: str
    sid: str
    snapshot: dict
    writable_roots: list[str]
    native_id: str | None = None
