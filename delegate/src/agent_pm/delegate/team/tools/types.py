"""Repository tool requests, execution services, checkout state, and command failures."""

from dataclasses import dataclass

from agent_pm.utils import Repo


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
