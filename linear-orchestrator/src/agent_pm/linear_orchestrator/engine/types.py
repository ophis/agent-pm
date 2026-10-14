"""The Linear orchestrator's data types used by two or more of its modules."""

import argparse
import re
from dataclasses import MISSING, dataclass, field, fields
from enum import StrEnum
from typing import Literal

from agent_pm.utils import Stopped as Stopped


class State(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    HANDOFF = "handoff"
    DONE = "done"
    CANCELED = "canceled"


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
    mapped: bool = False


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
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True, kw_only=True)
class Spec:
    uuid: str
    target: str | None = None
    issue: str
    project: str
    assignee: str
    sid: str
    task: str | None = None
    mode: str = field(metadata={"choices": ("new", "resume")})
    runtime: str
    input: str
    tui: bool = field(default=False, metadata={"action": "store_true"})
    split: str | None = None
    split_from: str | None = None
    opener: str | None = None
    events: str | None = None

    @classmethod
    def from_argv(cls, argv):
        parser = argparse.ArgumentParser(prog="run.py")
        for item in fields(cls):
            parser.add_argument(
                "--" + item.name.replace("_", "-"),
                required=item.default is MISSING,
                **item.metadata,
            )
        return cls(**vars(parser.parse_args(argv)))

    def to_argv(self) -> list[str]:
        argv = []
        for item in fields(self):
            value = vars(self)[item.name]
            if value is None or value is False:
                continue
            option = "--" + item.name.replace("_", "-")
            if value is True:
                argv.append(option)
            elif item.name in ("uuid", "target"):
                argv.extend((option, value))
            else:
                argv.append(f"{option}={value}")
        return argv

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
