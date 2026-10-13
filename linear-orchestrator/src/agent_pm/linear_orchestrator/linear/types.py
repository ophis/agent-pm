"""Values used while reporting agent runs to Linear."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Context:
    ident: str
    issue_id: str
    role: str
    sid: str
    resume: bool
    project: str | None
    workdir: str
    gql: Callable[..., dict]
    humans: tuple[str, ...]
    states: Mapping[str, str]
    repos: Mapping[str, str]
    team: str
    target: tuple[str, str] | None


@dataclass(frozen=True, kw_only=True)
class RunIdentity:
    ident: str
    issue_id: str
    sid: str
    resume: bool
    project: str | None
    target: tuple[str, str] | None = None
