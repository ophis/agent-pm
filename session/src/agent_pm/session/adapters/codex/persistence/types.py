"""Codex recovery records and failures shared by bundled runtime helpers."""

from __future__ import annotations

from dataclasses import dataclass


class RecoveryError(Exception):
    @classmethod
    def identity(cls) -> RecoveryError:
        return cls("invalid recovery state identity")


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
