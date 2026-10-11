"""The client interface: a Client turns a composed agent run into a Launch, a crew.py worker into a
WorkerCommand."""
import os
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

import repo
from compose import CONFIG, PROGRESS, RUN_KEYS, TEXT, ConfigError, RunConfig, RunParams, lookup  # noqa: F401

BLOCK = 1 << 16   # lines_back's read size


@dataclass(frozen=True)
class Access:
    """An agent run's client-neutral access intent; the selected client decides its effective grants."""
    dirs: list[str]       # combined extra dirs from read/write; effective access is client-specific
    commands: list[str]   # client interpretation: core/CLAUDE.md › An agent run's command
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
    session_state: dict | None = None  # opaque JSON recovery payload; the client validates its own fields
    writable_roots: list[str] = field(default_factory=list)  # effective writable locations, including client storage


@dataclass(frozen=True)
class Event:
    """One thing an agent run says: text to show (its stdout), a progress report (named by its point) or its outcome,
    both through report.py; or, from the driver, a line of the headless client's stderr (`stderr`), or a progress point
    the run never reported (`missing`, named by it); or a turn end (`stop`, the interactive client's Stop hook, whose
    `pending` counts the background work still running; meaningful on `stop` only)."""
    kind: Literal["text", "stderr", "progress", "outcome", "missing", "stop", "session"]
    text: str = ""
    name: str = ""
    outcome: dict | None = None
    pending: int = 0
    session_id: str = ""  # native client ID reported by a session event


@dataclass(frozen=True)
class SessionInfo:
    """Native session capabilities and current metadata, if the client can inspect them."""
    resume_supported: bool = False
    inspection_supported: bool = False
    client_id: str | None = None
    exists: bool = False
    transcript: str | None = None
    last_activity: float | None = None  # filesystem activity, seconds since epoch
    resume: str | None = None
    usage_supported: bool = False
    usage: dict | None = None


@dataclass(frozen=True)
class WorkerSpec:
    """A crew.py worker to start or restart; its client turns it into a WorkerCommand."""
    name: str
    sid: str                    # crew's session id
    cwd: str                    # absolute
    flags: tuple[str, ...]      # the client's own flags, verbatim
    prompt: str | None
    events: str                 # absolute, checked by tui_claude.events_file
    resume: bool
    env: Mapping[str, str]      # the worker's env after crew's environment policy
    exe: str | None = None      # restart: the stored executable; None: the client resolves its own on env["PATH"]


@dataclass(frozen=True)
class WorkerCommand:
    argv: list[str]                                     # argv[0] absolute; carries the client's lifecycle hooks
    env: dict[str, str] = field(default_factory=dict)   # set after crew's environment policy, before --env


class Client:
    keys = frozenset()     # this client's own keys; every config.toml run key (RUN_KEYS) is allowed too
    needs_config = True
    runs = True            # True: launch() starts an agent run; False: inline() gives the prompt to print instead
    inline_workdir = ""    # runs False: the prompt's <Workdir>, prose; "": no <Workdir> line
    inline_input = ""      # runs False: the prompt's # Input text; "": no # Input section
    session_env = ""       # the session marker: set in the commands this client's sessions run; non-empty, it is also
                           # an identity key of command_env
    command_env = MappingProxyType({})   # context variables: name → None (identity, any value) or an injected default
    worker_dialogs: tuple[str, ...] = ()   # screen texts of a worker's blocking dialogs that fire no hook

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

    def approval(self) -> str:
        """Client policy appended to the Parameters rule, including its punctuation."""
        return ""

    def report_interpreter(self) -> str:
        return "python3"

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

    def prepare_session(self, launch: Launch, params: RunParams) -> None:
        """Persist or verify recovery state before spawning a recoverable launch."""
        raise ConfigError(f"{type(self).__name__} does not support recovery state")

    def record_session(self, params: RunParams, session_id: str) -> None:
        """Persist the native ID before any later output or report is delivered."""
        raise ConfigError(f"{type(self).__name__} does not support native session recording")

    def resume_location(self, workdir: str, sid: str) -> tuple[str, bool] | None:
        """Authoritative saved cwd/project when this client has its own recovery state."""
        return None

    def session_info(self, workdir: str, sid: str) -> SessionInfo:
        """Native session capabilities and metadata; unsupported unless a client overrides it."""
        return SessionInfo()

    def inline(self, prompt: str) -> str:
        """The prompt for the calling conversation to follow."""
        raise NotImplementedError

    def worker(self, spec: WorkerSpec) -> WorkerCommand:
        """The command crew.py runs in a worker's pane; refuses the client's reserved flags. It may persist client
        state and spawn helper processes, so crew calls it last before the pane."""
        raise ConfigError(f"{type(self).__name__} has no worker command")

    def worker_reply(self, sid: str) -> str:
        """Worker session sid's last answer; "" when none yet."""
        raise ConfigError(f"{type(self).__name__} has no worker reply")


def client_tables(root: str) -> dict:
    """[clients] of <root>/config.toml with repo.LOCAL on top; {} when it is not a table."""
    path = os.path.join(root, CONFIG)
    if not os.path.isfile(path):
        raise ConfigError(f"no core config {path}")
    tables = repo.read_config(path).get("clients")
    return tables if isinstance(tables, dict) else {}


def load_config(name: str, root: str) -> dict:
    """[clients.<name>] of <root>/config.toml with repo.LOCAL on top."""
    table = client_tables(root).get(name)
    if not isinstance(table, dict):
        raise ConfigError(f"no [clients.{name}] table in {os.path.join(root, CONFIG)}")
    return table


def lines_back(f):
    """The binary file f's lines, last first, each without its newline; read from the end in BLOCK-byte reads."""
    pos = f.seek(0, os.SEEK_END)
    tail = []   # the pieces, last first, of the line whose start isn't read yet
    while pos > 0:
        size = min(BLOCK, pos)
        pos -= size
        f.seek(pos)
        parts = f.read(size).split(b"\n")
        tail.append(parts[-1])
        if len(parts) > 1:
            yield b"".join(reversed(tail))
            yield from reversed(parts[1:-1])
            tail = [parts[0]]
    yield b"".join(reversed(tail))

