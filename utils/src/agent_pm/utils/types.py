"""Typed failures shared by utility implementations and their callers."""

from enum import Enum


class ConfinedReason(Enum):
    REGULAR = "regular"
    SINGLE_LINK = "single_link"
    SUFFIX = "suffix"
    OUTSIDE = "outside"
    REPLACED = "replaced"
    TOO_LARGE = "too_large"


class ConfinedFileError(ValueError):
    def __init__(self, reason: ConfinedReason, path: str, *, limit: int | None = None):
        self.reason = reason
        self.path = path
        self.limit = limit
        super().__init__(
            f"{path!r}: {reason.value}" + (f" (limit {limit})" if limit is not None else "")
        )
