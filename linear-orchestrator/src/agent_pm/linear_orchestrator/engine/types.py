"""The Linear orchestrator's data types used by two or more of its modules."""

from dataclasses import dataclass
from typing import Literal

from agent_pm.utils import Stopped as Stopped

STATES = {
    "todo": "Todo",
    "in_progress": "In Progress",
    "in_review": "In Review",
    "handoff": "Handoff",
    "done": "Done",
    "canceled": "Canceled",
}


class ConfigError(Exception):
    def __init__(self, msg: str, logs_dir: str | None = None):
        super().__init__(msg)
        self.msg, self.logs_dir = msg, logs_dir


class LinearError(Exception):
    pass


class Unavailable(Exception):
    # its text never holds the request, headers or key
    def __init__(self, op, *, status=None, reason=None):
        super().__init__(f"{op}: {reason}" if reason is not None else f"{op}: HTTP {status}")
        self.op, self.status, self.reason = op, status, reason


class IssueError(LookupError):
    pass


class InputError(RuntimeError):
    pass


@dataclass(frozen=True)
class Invalid:
    reason: str


@dataclass(frozen=True)
class Transient:
    reason: str


@dataclass(frozen=True)
class Task:
    kind: Literal["research", "design", "build"]
    prefix: str = ""
    start: str = ""
    done: str = ""
    question: str = ""
    failed: str = ""
    failed_new: str = "in_review"
    retitle: bool = False
    approve: bool = False
    files: bool = False
    hint: str = ""


@dataclass(frozen=True)
class Target:
    owner: str
    name: str
    branch: str = ""
    clone: str = ""


ISSUE_ID = r"[A-Z][A-Z0-9]*-\d+"
