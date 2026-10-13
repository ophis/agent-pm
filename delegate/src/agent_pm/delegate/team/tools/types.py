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
    @classmethod
    def gh(cls, argv, cause):
        return cls(f"gh {' '.join(argv)[:100]}: {cause}")

    @classmethod
    def api(cls, cause):
        return cls(f"gh api: {cause}")

    @classmethod
    def repository_response(cls, repository: Repo, cause):
        return cls(f"gh api repos/{repository.owner}/{repository.name}: {cause!r}")

    @classmethod
    def clone(cls, cause):
        return cls(f"gh repo clone: {cause}")

    @classmethod
    def head(cls, revision):
        return cls(f"git rev-parse HEAD: {revision[:80]!r}")
