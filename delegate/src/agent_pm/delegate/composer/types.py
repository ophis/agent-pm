"""The Composer's shared types: ConfigError (the whole delegate's), a role's run config, one run's
params, its Team and a run's access intent.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from agent_pm.delegate.composer.errors.types import ConfigError as ConfigError
from agent_pm.delegate.engine.config import CONFIG
from agent_pm.utils import LOCAL

DELIVERABLE = os.path.join("tmp", "deliverable.md")  # under the workdir, without `out`
Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS = get_args(Effort)


@dataclass(frozen=True, kw_only=True)
class ConfigSource:
    path: str = CONFIG
    local: str = LOCAL
    table: Mapping | None = None


@dataclass(frozen=True, kw_only=True)
class RunOverrides:
    values: Sequence[Mapping] = ()
    source: str = "run overlay"
    table: str = "overlay"


@dataclass(frozen=True)
class RunLocation:
    cwd: str
    project: bool
    repo_dir: str | None = None


@dataclass(frozen=True, kw_only=True)
class RunConfig:
    role: str
    task: str = ""
    tier: int
    effort: Effort
    runtime: str = ""  # opaque: never compared with an Adapter's name
    output: dict
    read: list[str] = field(default_factory=list)
    write: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    gate: str = ""
    language: str = ""
    show: str = ""  # "": a pane; "none": nothing; else a command
    cwd: str = ""
    role_title: str = ""

    def __post_init__(self):
        if type(self.tier) is not int or not 1 <= self.tier <= 4:
            raise ConfigError.tier(self.tier)
        if self.effort not in EFFORTS:
            raise ConfigError.effort(EFFORTS, self.effort)
        if (
            not isinstance(self.runtime, str)
            or not self.runtime.strip()
            or not self.runtime.isprintable()
        ):
            raise ConfigError.runtime(self.runtime)
        if "type" not in self.output:
            raise ConfigError.output_type()
        if not isinstance(self.gate, str) or "\n" in self.gate or "`" in self.gate:
            raise ConfigError.gate()
        if not isinstance(self.language, str) or "\n" in self.language:
            raise ConfigError.language()
        if not isinstance(self.show, str) or "\n" in self.show:
            raise ConfigError.show()
        if self.cwd and (
            not isinstance(self.cwd, str)
            or not self.cwd.isprintable()
            or not os.path.isabs(os.path.expanduser(self.cwd))
        ):
            raise ConfigError.configured_cwd(self.cwd)


@dataclass(frozen=True, kw_only=True)
class RunParams:
    input: str
    out: str
    workdir: str
    resume: bool = False

    @property
    def deliverable(self) -> str:
        out = os.path.abspath(self.out)
        if Path(os.path.realpath(out)).is_relative_to(os.path.realpath(self.workdir)):
            return out
        return os.path.join(os.path.abspath(self.workdir), DELIVERABLE)


@dataclass(frozen=True)
class Team:
    """The roles, tasks and templates a run can name: the team root's and the team dirs' files
    (composer.team).
    """

    root: str
    dirs: tuple[str, ...] = ()
    files: Mapping[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    misnamed: tuple[str, ...] = ()

    def custom(self, path: str) -> bool:
        return not Path(path).is_relative_to(self.root)

    def label(self, kind: str, name: str, path: str) -> str:
        return path if self.custom(path) else f"{kind}/{name}.md"

    def source(self, path: str) -> str:
        return path if self.custom(path) else "built-in"


@dataclass(frozen=True)
class Intent:
    dirs: tuple[str, ...]
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    commands: tuple[str, ...]
    gate: str
    env: Mapping[str, str]
    cwd: str
    project: bool
