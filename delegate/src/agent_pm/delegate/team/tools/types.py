"""Repository tool requests, execution services, checkout state, and command failures."""

import sys
from dataclasses import dataclass
from typing import TextIO

from agent_pm.utils import Repo, Runner, sh


@dataclass(frozen=True)
class CheckoutRequest:
    repository: str
    branch: str
    directory: str
    slug: str | None = None
    base_branch: str | None = None


@dataclass(frozen=True, kw_only=True)
class RepoServices:
    run: Runner = sh
    out: TextIO = sys.stdout
    err: TextIO = sys.stderr


@dataclass(frozen=True)
class Worktree:
    repository: Repo
    clone: str | None
    path: str
    exists: bool
    symlinks: str


@dataclass(frozen=True)
class BranchSelection:
    options: tuple[str, ...]
    start: str
    registered: bool


class RepoCommandError(RuntimeError):
    pass
