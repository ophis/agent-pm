"""The Linear orchestrator's data types used by two or more of its modules."""

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from agent_pm import delegate
from agent_pm.session import Driver

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


class Stopped(BaseException):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Unavailable(Exception):
    # its text never holds the request, headers or key
    def __init__(self, op, *, status=None, reason=None):
        super().__init__(f"{op}: {reason}" if reason is not None else f"{op}: HTTP {status}")
        self.op, self.status, self.reason = op, status, reason


class IssueError(LookupError):
    pass


class FileError(ValueError):
    pass


class TuiError(ValueError):
    pass


class InputError(RuntimeError):
    pass


CALL_ERRORS = (
    LinearError,
    OSError,
    ValueError,
    LookupError,
    TypeError,
    RuntimeError,
    subprocess.SubprocessError,
)

SERVICE_ERRORS = (*CALL_ERRORS, Unavailable)


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
class Role:
    account: str
    key: str
    tasks: tuple[str, ...]
    max_runs: int
    next: str | None
    require_instructions: bool
    run: delegate.RunConfig


@dataclass(frozen=True)
class Docs:
    repo: str
    branch: str
    dirs: Mapping[str, str]


@dataclass(frozen=True)
class Config:
    team: str
    states: Mapping[str, str]
    human_members: tuple[str, ...]
    harness_key: str
    task_label_group: str
    task_labels: Mapping[str, str]
    roles: Mapping[str, Role]
    project_repos: Mapping[str, str]
    local_clones: Mapping[str, str]
    work_dir: str
    docs: Docs
    stages: Mapping[str, int]
    driver: Driver

    @property
    def runs_dir(self) -> str:
        return os.path.join(self.work_dir, "work")

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.work_dir, "logs")

    @property
    def runs_log(self) -> str:
        return os.path.join(self.logs_dir, "runs.jsonl")

    def run_dir(self, issue: str) -> str:
        return os.path.join(self.runs_dir, issue)


@dataclass
class Promotion:
    source: dict[str, Any]
    detail: dict[str, Any]
    role: str
    next: str
    cutoff: str | None
    first: str
    latest: str
    child: dict[str, Any] | None = None


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    states: dict[str, str]


@dataclass(frozen=True)
class Target:
    owner: str
    name: str
    branch: str = ""
    clone: str = ""


@dataclass(frozen=True)
class Doc:
    url: str
    path: str
    text: str | None


@dataclass(frozen=True)
class Sources:
    docs: tuple[Doc, ...]
    earlier: Doc | None


ISSUE_ID = r"[A-Z][A-Z0-9]*-\d+"
