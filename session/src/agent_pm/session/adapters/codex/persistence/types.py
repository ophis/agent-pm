"""RecoveryError failures raised by the bundled session runtime helpers."""

from __future__ import annotations


class RecoveryError(Exception):
    @classmethod
    def identity(cls) -> RecoveryError:
        return cls("invalid recovery state identity")
