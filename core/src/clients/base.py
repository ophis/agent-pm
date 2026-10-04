"""The client interface: a Client turns a composed run into a Launch."""
import os
import tomllib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal

from compose import PROGRESS, RUN_KEYS, TEXT, ConfigError, RunConfig, RunParams, lookup  # noqa: F401


@dataclass(frozen=True)
class Access:
    """A run's client-neutral constraints, as absolute paths and commands to pre-approve."""
    dirs: list[str]       # extra dirs the run may reach
    commands: list[str]   # shell commands to pre-approve


@dataclass(frozen=True)
class Launch:
    argv: list[str]                                        # empty: nothing to start
    env: dict[str, str] = field(default_factory=dict)      # added to the caller's environment
    cwd: str = ""
    files: dict[str, str] = field(default_factory=dict)    # path → text, written by the driver
    interactive: list[str] = field(default_factory=list)   # the same run's interactive command; empty: none


@dataclass(frozen=True)
class Event:
    """One thing a run says: text to show (its stdout), a progress report (named by its point) or its outcome (the
    last one counts), both through report.py; or, from the driver, a progress point the run never reported
    (`missing`, named by it)."""
    kind: Literal["text", "progress", "outcome", "missing"]
    text: str = ""
    name: str = ""
    outcome: dict | None = None


class Client:
    keys = frozenset()     # this client's own keys; every config.toml run key (RUN_KEYS) is allowed too
    needs_config = True
    runs = True            # True: launch() starts a run; False: export() writes files instead

    def __init__(self, config: dict):
        if extra := sorted(set(config) - self.keys - RUN_KEYS):
            raise ConfigError(f"unknown key {extra[0]!r} in {type(self).__name__}'s config")
        self.config = config

    def scripts_path(self, root: str) -> str:
        """How prompts name core's src/ dir: its absolute path unless the client says otherwise."""
        return os.path.join(os.path.abspath(root), "src")

    def methods_path(self, root: str) -> str:
        """How prompts name core's team/methods/ dir: its absolute path unless the client says otherwise."""
        return os.path.join(os.path.abspath(root), TEXT, "methods")

    def handover(self) -> str:
        """Prompt text (Output › Return): how the run returns its outcome and reports progress; `{{report}}` is
        filled with the report command (compose.report_command)."""
        return ""

    def value(self, run: RunConfig, key: str):
        """One of this client's own keys for the run, in config.toml's layout."""
        return lookup(self.config, run.role, run.task, key)

    def launch(self, prompt: str, run: RunConfig, *, params: RunParams, access: Access) -> Launch:
        """The command for a run."""
        raise NotImplementedError

    def events(self, lines: Iterable[str]) -> Iterator[Event]:
        """The run's stdout as text Events."""
        for line in lines:
            yield Event("text", line.rstrip("\n"))

    def export(self, prompt: str, run: RunConfig, *, dest: str) -> Launch:
        raise NotImplementedError


def load_config(name: str, root: str) -> dict:
    path = os.path.join(root, "config", "clients", f"{name}.toml")
    if not os.path.isfile(path):
        raise ConfigError(f"no client config {path}")
    with open(path, "rb") as f:
        return tomllib.load(f)
