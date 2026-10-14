"""Outcome data and validation failures shared by the delegate's callers."""

from dataclasses import dataclass, field
from enum import StrEnum


class OutcomeStatus(StrEnum):
    DONE = "done"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"


STATUSES = tuple(OutcomeStatus)


class InvalidOutcome(Exception):
    pass


@dataclass(frozen=True)
class Outcome:
    status: OutcomeStatus
    title: str
    summary: str
    questions: list[str] = field(default_factory=list)
    url: str = ""
    files: list[str] = field(default_factory=list)
    deliverable: str = ""
