"""Outcome data and validation failures shared by the delegate's callers."""

from dataclasses import dataclass, field
from enum import StrEnum


class OutcomeStatus(StrEnum):
    DONE = "done"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"


STATUSES = tuple(OutcomeStatus)


class AutopilotStatus(StrEnum):
    CONVERGED = "converged"
    CAPPED = "capped-without-pass"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Autopilot:
    """An outcome's `autopilot`: the status and head of the run's last autopilot result."""

    status: AutopilotStatus
    head: str


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
