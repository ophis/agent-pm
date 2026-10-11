"""The client interface: a Client turns a composed agent run into a Launch."""
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal

import repo
from compose import CONFIG, PROGRESS, RUN_KEYS, TEXT, ConfigError, RunConfig, RunParams, lookup  # noqa: F401


@dataclass(frozen=True)
class Access:
    """An agent run's client-neutral constraints, as absolute paths and commands to pre-approve."""
    dirs: list[str]       # extra dirs the agent run may reach
    commands: list[str]   # shell commands to pre-approve
    cwd: str = ""         # the dir the agent run starts in (drive.place)
    project: bool = False   # whether the client loads cwd's project settings and instructions (drive.place)


@dataclass(frozen=True)
class Launch:
    argv: list[str]                                        # empty: nothing to start
    env: dict[str, str] = field(default_factory=dict)      # added to the caller's environment
    cwd: str = ""
    interactive: list[str] = field(default_factory=list)   # the same agent run's interactive command; empty: none
    transcript: str = ""   # where the client saves the session; "": unknown
    resume: str = ""       # the shell command a human resumes the session with; "": none
    project: bool = False  # Access.project, set by drive.plan
    status_line: bool = False   # the config's status_line, set by drive.plan, for the tui runner
    per_column: int | None = None   # the config's workers_per_column, set by drive.plan, for the tui runner
    retile: str | None = None       # the config's grid_retile, set by drive.plan, for the tui runner


@dataclass(frozen=True)
class Event:
    """One thing an agent run says: text to show (its stdout), a progress report (named by its point) or its outcome,
    both through report.py; or, from the driver, a line of the headless client's stderr (`stderr`), or a progress point
    the run never reported (`missing`, named by it); or a turn end (`stop`, the interactive client's Stop hook, whose
    `pending` counts the background work still running; meaningful on `stop` only)."""
    kind: Literal["text", "stderr", "progress", "outcome", "missing", "stop"]
    text: str = ""
    name: str = ""
    outcome: dict | None = None
    pending: int = 0


class Client:
    keys = frozenset()     # this client's own keys; every config.toml run key (RUN_KEYS) is allowed too
    needs_config = True
    runs = True            # True: launch() starts an agent run; False: inline() gives the prompt to print instead
    inline_workdir = ""    # runs False: the prompt's <Workdir>, prose; "": no <Workdir> line
    inline_input = ""      # runs False: the prompt's # Input text; "": no # Input section

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

    def tasks_path(self, root: str) -> str:
        """How prompts name core's team/tasks/ dir: its absolute path unless the client says otherwise."""
        return os.path.join(os.path.abspath(root), TEXT, "tasks")

    def handover(self) -> str:
        """Prompt text (Output › Return): how the agent run returns its outcome and reports progress; a code span
        starting with `report` names the report command (compose.PARAMETERS)."""
        return ""

    def value(self, run: RunConfig, key: str):
        """One of this client's own keys for the agent run, in config.toml's layout."""
        return lookup(self.config, run.role, key)

    def launch(self, prompt: str, run: RunConfig, *, params: RunParams, access: Access) -> Launch:
        """The command for an agent run."""
        raise NotImplementedError

    def events(self, lines: Iterable[str]) -> Iterator[Event]:
        """The agent run's stdout as text Events."""
        for line in lines:
            yield Event("text", line.rstrip("\n"))

    def inline(self, prompt: str) -> str:
        """The prompt for the calling conversation to follow."""
        raise NotImplementedError


def load_config(name: str, root: str) -> dict:
    """[clients.<name>] of <root>/config.toml with repo.LOCAL on top."""
    path = os.path.join(root, CONFIG)
    if not os.path.isfile(path):
        raise ConfigError(f"no core config {path}")
    tables = repo.read_config(path).get("clients")
    table = tables.get(name) if isinstance(tables, dict) else None
    if not isinstance(table, dict):
        raise ConfigError(f"no [clients.{name}] table in {path}")
    return table
