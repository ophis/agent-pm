"""Codex native session metadata and validated recovery state."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class Snapshot:
    cwd: str
    project: bool
    dirs: tuple[str, ...]
    commands: tuple[str, ...]
    model: str | None
    effort: str | None
    codex_home: str
    flags: tuple[str, ...]
    env: Mapping[str, str]

    def __post_init__(self):
        if not isinstance(self.cwd, str) or not isinstance(self.codex_home, str):
            raise ValueError("snapshot paths must be strings")
        if type(self.project) is not bool:
            raise ValueError("snapshot project must be a boolean")
        for value in (self.model, self.effort):
            if value is not None and not isinstance(value, str):
                raise ValueError("snapshot model and effort must be strings or None")
        for name, values in (
            ("dirs", self.dirs),
            ("commands", self.commands),
            ("flags", self.flags),
        ):
            copied = tuple(values)
            if any(not isinstance(value, str) for value in copied):
                raise ValueError(f"snapshot {name} must contain strings")
            object.__setattr__(self, name, copied)
        env = dict(self.env)
        if any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()
        ):
            raise ValueError("snapshot env must map strings to strings")
        object.__setattr__(self, "env", MappingProxyType(env))


@dataclass(frozen=True)
class NativeMetadata:
    id: str
    cwd: str
    parent_thread_id: str | None

    def __post_init__(self):
        if not isinstance(self.id, str) or not isinstance(self.cwd, str):
            raise ValueError("native session identity must contain strings")
        if self.parent_thread_id is not None and not isinstance(self.parent_thread_id, str):
            raise ValueError("native session parent must be a string or None")


@dataclass(frozen=True)
class SessionFile:
    path: str
    mtime: float
    metadata: NativeMetadata

    def __post_init__(self):
        if not isinstance(self.path, str) or not isinstance(self.mtime, int | float):
            raise ValueError("native session file must contain a path and timestamp")
        if not isinstance(self.metadata, NativeMetadata):
            raise ValueError("native session metadata must be NativeMetadata")


@dataclass(frozen=True)
class SessionState:
    workdir: str
    sid: str
    snapshot: Snapshot
    writable_roots: tuple[str, ...]
    native_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.workdir, str) or not isinstance(self.sid, str):
            raise ValueError("session identity must contain strings")
        if self.native_id is not None and not isinstance(self.native_id, str):
            raise ValueError("native session ID must be a string or None")
        if not isinstance(self.snapshot, Snapshot):
            raise ValueError("session snapshot must be Snapshot")
        roots = tuple(self.writable_roots)
        if any(not isinstance(root, str) for root in roots):
            raise ValueError("session writable roots must contain strings")
        object.__setattr__(self, "writable_roots", roots)


@dataclass(frozen=True)
class HookPlan:
    argv: tuple[str, ...]
    import_roots: tuple[str, ...]

    def __post_init__(self):
        for name, values in (("argv", self.argv), ("import_roots", self.import_roots)):
            copied = tuple(values)
            if any(not isinstance(value, str) for value in copied):
                raise ValueError(f"hook plan {name} must contain strings")
            object.__setattr__(self, name, copied)


@dataclass(frozen=True)
class LaunchPlan:
    executable: str
    selection: tuple[str, ...]
    prompt: str
    hook: HookPlan
    workdir: str
    sid: str
    store: str
    protect: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.hook, HookPlan):
            raise ValueError("launch hook must be HookPlan")
        if any(
            not isinstance(value, str)
            for value in (self.executable, self.prompt, self.workdir, self.sid, self.store)
        ):
            raise ValueError(
                "launch plan executable, prompt, identity and store must contain strings"
            )
        for name, values in (
            ("selection", self.selection),
            ("protect", self.protect),
        ):
            copied = tuple(values)
            if any(not isinstance(value, str) for value in copied):
                raise ValueError(f"launch plan {name} must contain strings")
            object.__setattr__(self, name, copied)
        if any(word.startswith("-") for word in self.selection):
            raise ValueError("Codex tui launch: invalid session selection")
