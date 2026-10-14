"""The Composer's shared types: ConfigError (the whole delegate's), a role's run config, one run's
params, its Team and a run's access intent.
"""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agent_pm.session import Effort
from agent_pm.utils import freeze

DELIVERABLE = os.path.join("tmp", "deliverable.md")  # under the workdir, without `out`


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class DelegateConfig:
    values: Mapping

    def __post_init__(self):
        object.__setattr__(self, "values", freeze(self.values))


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
    cwd: str = ""
    role_title: str = ""
    idle_timeout: float = 7200

    def __post_init__(self):
        if (
            isinstance(self.idle_timeout, bool)
            or not isinstance(self.idle_timeout, (int, float))
            or not math.isfinite(self.idle_timeout)
            or self.idle_timeout <= 0
        ):
            raise ConfigError(
                f"idle_timeout must be a positive finite number of seconds, got {self.idle_timeout!r}"
            )
        if type(self.tier) is not int or not 1 <= self.tier <= 4:
            raise ConfigError(f"tier must be an integer 1–4, got {self.tier!r}")
        try:
            object.__setattr__(self, "effort", Effort(self.effort))
        except ValueError:
            raise ConfigError(
                f"effort must be one of {', '.join(Effort)}, got {self.effort!r}"
            ) from None
        if (
            not isinstance(self.runtime, str)
            or not self.runtime.strip()
            or not self.runtime.isprintable()
        ):
            raise ConfigError(
                f"runtime must be a non-empty one-line name, got {self.runtime!r:.80}"
            )
        if "type" not in self.output:
            raise ConfigError("output.type is not set")
        if not isinstance(self.gate, str) or "\n" in self.gate or "`" in self.gate:
            raise ConfigError("gate must be one line of shell command without backticks")
        if not isinstance(self.language, str) or "\n" in self.language:
            raise ConfigError("language must be one line of text")
        if self.cwd and (
            not isinstance(self.cwd, str)
            or not self.cwd.isprintable()
            or not os.path.isabs(os.path.expanduser(self.cwd))
        ):
            raise ConfigError(f"cwd must be a printable absolute or ~ path, got {self.cwd!r:.80}")


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
