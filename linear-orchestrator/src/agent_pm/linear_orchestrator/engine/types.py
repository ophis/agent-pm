"""The Linear orchestrator's data types used by two or more of its modules."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from agent_pm import delegate
from agent_pm.linear_orchestrator.engine.errors.types import (
    CALL_ERRORS as CALL_ERRORS,
)
from agent_pm.linear_orchestrator.engine.errors.types import (
    SERVICE_ERRORS as SERVICE_ERRORS,
)
from agent_pm.linear_orchestrator.engine.errors.types import (
    ConfigError as ConfigError,
)
from agent_pm.linear_orchestrator.engine.errors.types import (
    FileError as FileError,
)
from agent_pm.linear_orchestrator.engine.errors.types import (
    InputError as InputError,
)
from agent_pm.linear_orchestrator.engine.errors.types import (
    IssueError as IssueError,
)
from agent_pm.linear_orchestrator.engine.errors.types import (
    LinearError as LinearError,
)
from agent_pm.linear_orchestrator.engine.errors.types import (
    Stopped as Stopped,
)
from agent_pm.linear_orchestrator.engine.errors.types import (
    TuiError as TuiError,
)
from agent_pm.linear_orchestrator.engine.errors.types import (
    Unavailable as Unavailable,
)
from agent_pm.session import Driver


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
class CoreConfig:
    path: str
    local: str
    values: dict[str, Any]


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
