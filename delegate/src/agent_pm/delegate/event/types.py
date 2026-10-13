"""Outcome data and validation failures shared by the delegate's callers."""

from dataclasses import dataclass, field
from typing import Literal, get_args

OutcomeStatus = Literal["done", "needs_input", "failed"]
STATUSES = get_args(OutcomeStatus)


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
