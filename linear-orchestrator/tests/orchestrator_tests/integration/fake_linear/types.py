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
