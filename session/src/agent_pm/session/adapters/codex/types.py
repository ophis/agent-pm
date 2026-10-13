"""Codex native session metadata and validated recovery state."""

from dataclasses import dataclass


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
