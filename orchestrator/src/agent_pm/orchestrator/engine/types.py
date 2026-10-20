"""The Linear orchestrator's data types used by two or more of its modules."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from agent_pm import delegate
from agent_pm.orchestrator.engine.flow.types import ISSUE_ID, Found
from agent_pm.utils import Stopped as Stopped

ClientSource = Literal["label", "default"]


@dataclass(frozen=True)
class Role:
    account: str
    key: str
    tasks: tuple[str, ...]
    max_runs: int
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
    task_tiers: Mapping[str, int]
    agent_label_group: str
    agent_labels: Mapping[str, str]
    client_max_runs: Mapping[str, int]
    roles: Mapping[str, Role]
    project_repos: Mapping[str, str]
    local_clones: Mapping[str, str]
    work_dir: str
    docs: Docs
    stages: Mapping[str, int]
    delegate_kind: str
    delegates: Mapping[str, type]
    core: Mapping

    @property
    def delegate_cls(self) -> type:
        return self.delegates[self.delegate_kind]

    @property
    def workdirs(self) -> str:
        return os.path.join(self.work_dir, "work")

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.work_dir, "logs")

    @property
    def runs_log(self) -> str:
        return os.path.join(self.logs_dir, "runs.jsonl")

    def issue_workdir(self, issue: str) -> str:
        return os.path.join(self.workdirs, issue)

    def task_tier(self, task: str | None) -> int | None:
        """The [task_tiers] tier of task, else None: the run keeps its Delegate tier."""
        return None if task is None else self.task_tiers.get(task)


class State(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    HANDOFF = "handoff"
    DONE = "done"
    CANCELED = "canceled"


class Resource(StrEnum):
    """What agent-pm-orchestrator issue lookup lists."""

    TEAMS = "teams"
    STATES = "states"
    LABELS = "labels"
    PROJECTS = "projects"
    MILESTONES = "milestones"
    USERS = "users"


class FinishedType(StrEnum):
    """Linear state types finished beside the configured done state: Duplicate is canceled-type."""

    CANCELED = "canceled"
    DUPLICATE = "duplicate"


class ConfigError(Exception):
    def __init__(self, msg: str, logs_dir: str | None = None):
        super().__init__(msg)
        self.msg, self.logs_dir = msg, logs_dir


class LinearError(Exception):
    pass


class KeychainError(LinearError):
    """The harness_key Keychain item holds no usable key."""


class GraphQLErrors(LinearError):
    """A response's GraphQL errors, kept as .errors (client.messages gives each one's text)."""

    def __init__(self, errors):
        super().__init__(f"linear api error: {errors}")
        self.errors = errors


class WriteUnknown(LinearError):
    """A write Linear never answered: it may or may not have landed."""


class Unavailable(Exception):
    # its text never holds the request, headers or key
    def __init__(self, op, *, status=None, reason=None):
        super().__init__(f"{op}: {reason}" if reason is not None else f"{op}: HTTP {status}")
        self.op, self.status, self.reason = op, status, reason


class IssueError(LookupError):
    pass


class ArchiveRefused(Exception):
    """An archive the issue's or a descendant's unfinished state blocks; nothing was written."""


class LabelRefused(Exception):
    """A label swap the issue's current labels block; nothing was written."""


class RelationsFailed(Exception):
    """create made the issue but not every blocking relation: .report holds the receipt, done,
    failed and the recover commands adding only the failed ones.
    """

    def __init__(self, report):
        self.report = report
        failed, total = len(report["failed"]), len(report["failed"]) + len(report["done"])
        issue = report["issue"]
        super().__init__(
            f"create {issue['identifier']}: created {issue['url']}, but {failed} of {total} "
            f"blocking relations failed; add only them: {'; '.join(report['recover'])}; "
            "do not run create again"
        )


class InputError(RuntimeError):
    pass


@dataclass(frozen=True)
class Ref:
    """A first-claim search's doc or merged PR; owner: the Found it belongs to, if any."""

    label: str
    url: str
    owner: str | None


@dataclass(frozen=True)
class Related:
    issues: tuple[Found, ...] = ()
    refs: tuple[Ref, ...] = ()


@dataclass(frozen=True)
class Invalid:
    reason: str
    mapped: bool = False


@dataclass(frozen=True)
class Transient:
    reason: str


@dataclass(frozen=True)
class Target:
    owner: str
    name: str
    branch: str = ""
    clone: str = ""


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Claim:
    """A run's claim context as its sid's runs.jsonl start row recorded it; `key`: the role's
    Keychain service then, its write-back's Linear account.
    """

    issue: str
    sid: str
    role: str
    uuid: str
    project: str | None
    target: tuple[str, str] | None
    key: str
    client: str
    delegate_kind: str


@dataclass(frozen=True, kw_only=True)
class Spec:
    """A run the router claimed or resumes: its issue's identity, its session and client."""

    uuid: str
    issue: str
    project: str
    assignee: str
    sid: str
    task: str | None = None
    mode: str
    client: str
    client_source: ClientSource = "default"

    @classmethod
    def from_issue(cls, issue, *, mode, sid, task, client, client_source: ClientSource = "default"):
        return cls(
            uuid=issue["id"],
            issue=issue["identifier"],
            project=issue["project"]["id"],
            assignee=issue["assignee"]["email"],
            sid=sid,
            task=task,
            mode=mode,
            client=client,
            client_source=client_source,
        )

    def identity_error(self) -> str | None:
        if not re.fullmatch(ISSUE_ID, self.issue) or not UUID_RE.fullmatch(self.sid):
            return f"bad issue or session id: {self.issue} {self.sid}"
        return None

    def role_error(self, role: str | None, tasks: tuple[str, ...]) -> str | None:
        if role is None:
            return f"{self.assignee!r} is not a role account"
        if self.task is not None and self.task not in tasks:
            return f"task {self.task!r} is not one of {role}'s tasks ({', '.join(tasks)})"
        return None
