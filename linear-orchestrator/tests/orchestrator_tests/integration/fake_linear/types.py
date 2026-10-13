from dataclasses import dataclass


@dataclass
class Request:
    op: str | None
    account: str | None
    variables: dict
    result: str


@dataclass
class _Failure:
    op: str
    kind: str
    account: str | None
    times: int
    seconds: float


@dataclass(frozen=True, kw_only=True)
class IssueSeed:
    identifier: str | None = None
    id: str | None = None
    state: str = "todo"
    assignee: str | None = None
    project: dict | None = None
    description: str = ""
    priority: int = 0
    labels: tuple = ()
    created: str | None = None
    updated: str | None = None


@dataclass(frozen=True)
class HttpReply:
    status: int
    payload: dict
    delay: float = 0
