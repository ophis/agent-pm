import os
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

import repo
from compose import CONFIG, PROGRESS, RUN_KEYS, TEXT, ConfigError, RunConfig, RunParams, lookup  # noqa: F401

BLOCK = 1 << 16


@dataclass(frozen=True)
class Access:
    dirs: list[str]
    commands: list[str]
    cwd: str = ""
    project: bool = False


@dataclass(frozen=True)
class Launch:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    interactive: list[str] = field(default_factory=list)
    transcript: str = ""
    resume: str = ""
    project: bool = False
    status_line: bool = False
    per_column: int | None = None
    retile: str | None = None
    session_state: dict | None = None
    writable_roots: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Event:
    kind: Literal["text", "stderr", "progress", "outcome", "missing", "stop", "session"]
    text: str = ""
    name: str = ""
    outcome: dict | None = None
    pending: int = 0
    session_id: str = ""


@dataclass(frozen=True)
class SessionInfo:
    resume_supported: bool = False
    inspection_supported: bool = False
    client_id: str | None = None
    exists: bool = False
    transcript: str | None = None
    last_activity: float | None = None
    resume: str | None = None
    usage_supported: bool = False
    usage: dict | None = None


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    sid: str
    cwd: str
    flags: tuple[str, ...]
    prompt: str | None
    events: str
    resume: bool
    env: Mapping[str, str]
    exe: str | None = None


@dataclass(frozen=True)
class WorkerCommand:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


class Client:
    keys = frozenset()
    needs_config = True
    runs = True
    inline_workdir = ""
    inline_input = ""
    session_env = ""
    command_env = MappingProxyType({})
    worker_dialogs: tuple[str, ...] = ()

    def __init__(self, config: dict):
        if extra := sorted(set(config) - self.keys - RUN_KEYS):
            raise ConfigError(f"unknown key {extra[0]!r} in {type(self).__name__}'s config")
        self.config = config

    def scripts_path(self, root: str) -> str:
        return os.path.join(os.path.abspath(root), "src")

    def methods_path(self, root: str) -> str:
        return os.path.join(os.path.abspath(root), TEXT, "methods")

    def tasks_path(self, root: str) -> str:
        return os.path.join(os.path.abspath(root), TEXT, "tasks")

    def approval(self) -> str:
        return ""

    def report_interpreter(self) -> str:
        return "python3"

    def handover(self) -> str:
        return ""

    def value(self, run: RunConfig, key: str):
        return lookup(self.config, run.role, key)

    def launch(self, prompt: str, run: RunConfig, *, params: RunParams, access: Access) -> Launch:
        raise NotImplementedError

    def events(self, lines: Iterable[str]) -> Iterator[Event]:
        for line in lines:
            yield Event("text", line.rstrip("\n"))

    def prepare_session(self, launch: Launch, params: RunParams) -> None:
        raise ConfigError(f"{type(self).__name__} does not support recovery state")

    def record_session(self, params: RunParams, session_id: str) -> None:
        raise ConfigError(f"{type(self).__name__} does not support native session recording")

    def resume_location(self, workdir: str, sid: str) -> tuple[str, bool] | None:
        return None

    def session_info(self, workdir: str, sid: str) -> SessionInfo:
        return SessionInfo()

    def inline(self, prompt: str) -> str:
        raise NotImplementedError

    def worker(self, spec: WorkerSpec) -> WorkerCommand:
        raise ConfigError(f"{type(self).__name__} has no worker command")

    def worker_reply(self, sid: str) -> str:
        raise ConfigError(f"{type(self).__name__} has no worker reply")


def client_tables(root: str) -> dict:
    path = os.path.join(root, CONFIG)
    if not os.path.isfile(path):
        raise ConfigError(f"no core config {path}")
    tables = repo.read_config(path).get("clients")
    return tables if isinstance(tables, dict) else {}


def load_config(name: str, root: str) -> dict:
    table = client_tables(root).get(name)
    if not isinstance(table, dict):
        raise ConfigError(f"no [clients.{name}] table in {os.path.join(root, CONFIG)}")
    return table


def lines_back(f):
    pos = f.seek(0, os.SEEK_END)
    tail = []
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
