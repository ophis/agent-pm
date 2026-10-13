"""The Composer's shared types: its ConfigError, a role's run config and one run's params."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS = get_args(Effort)


class ConfigError(Exception):
    pass


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
            raise ConfigError(f"tier must be an integer 1–4, got {self.tier!r}")
        if self.effort not in EFFORTS:
            raise ConfigError(f"effort must be one of {', '.join(EFFORTS)}, got {self.effort!r}")
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
        if not isinstance(self.show, str) or "\n" in self.show:
            raise ConfigError("show must be one line of shell command")
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
        return os.path.join(os.path.abspath(self.workdir), "tmp", "deliverable.md")
