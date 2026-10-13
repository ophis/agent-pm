"""The manager's types: crew.py's Spec, crew.py's and roster.py's errors."""

import argparse
import errno
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    """One worker for crew.start: `resume` the session id it resumes; `client` None: the caller's
    runtime, else the config's; `overrides` KEY=VALUE env entries; `note` its roster note.
    """

    name: str
    cwd: str
    prompt: str | None = None
    note: str | None = None
    flags: tuple[str, ...] = ()
    client: str | None = None
    overrides: tuple[str, ...] = ()
    resume: str | None = None


UNPRINTABLE = "want printable text (no control character, U+2028 or U+2029)"
ROTATE_MSG = "rotate: run crew.py attach, then arm with its N and GEN"


class RosterError(Exception):
    @classmethod
    def caller_unknown(cls, cause: Exception):
        return cls(f"{cause}; give --manager <name>")

    @classmethod
    def manager_name(cls, name: str):
        return cls(f"manager {name!r}: want [A-Za-z0-9_-]+")

    @classmethod
    def not_attached(cls, directory: str):
        return cls(f"manager directory {directory} not attached: run crew.py attach first")

    @classmethod
    def directory_io(cls, kind: str, path: str, cause: OSError):
        return cls(f"{kind} {path}: {cause.strerror}")

    @classmethod
    def directory_owner(cls, kind: str, path: str):
        return cls(f"{kind} {path}: not a directory owned by you")

    @classmethod
    def directory_permissions(cls, kind: str, path: str, mode: int):
        return cls(f"{kind} {path}: group or other permission bits set (mode {mode:04o})")

    @classmethod
    def worker_sid(cls, sid: object):
        return cls(f"worker sid {sid!r}: want a session id")

    @classmethod
    def configuration(cls, cause: Exception):
        return cls(f"config: {cause}")

    @classmethod
    def missing_runtime(cls):
        return cls("config: runtime: want a runtime name")

    @classmethod
    def schema_fault(cls, path: str, diagnostic: str):
        return cls(f"{path}: {diagnostic}")

    @classmethod
    def file_io(cls, path: str, cause: OSError):
        if cause.errno == errno.ELOOP:
            return cls.file_owner(path)
        return cls(f"{path}: {cause.strerror}")

    @classmethod
    def file_owner(cls, path: str):
        return cls(f"{path}: not a regular file owned by you")

    @classmethod
    def busy(cls, path: str):
        return cls(f"{path}: busy")

    @classmethod
    def too_large(cls, path: str, maximum: int):
        return cls(f"{path}: over {maximum} bytes")

    @classmethod
    def invalid_json(cls, path: str, cause: ValueError | None = None):
        return cls(f"{path}: invalid JSON: {cause if cause is not None else 'nested too deep'}")

    @classmethod
    def invalid_entry(cls, diagnostic: str, name: str | None = None):
        subject = "entry" if name is None else f"entry {name!r}"
        return cls(f"{subject}: {diagnostic}")

    @classmethod
    def missing_entry(cls, name: str):
        return cls(f"{name} not in roster")

    @classmethod
    def kill_failed(cls, name: str, session: str):
        return cls(f"{name}: session {session} still live after tmux kill-session")

    @classmethod
    def file_missing(cls, path: str):
        return cls(f"{path}: {os.strerror(errno.ENOENT)}")


class Stale(RosterError):
    @classmethod
    def generation(cls, requested: int | None, current: int):
        return cls(f"stale line numbers (gen {requested}, now {current}): run crew.py attach")

    @classmethod
    def past_end(cls, line: int, path: str, total: int):
        return cls(f"line {line} is past the end of {path} ({total} lines): run crew.py attach")


class Held(RosterError):
    def __init__(self, directory: str, session: str, since: str):
        super().__init__(
            f"manager directory {directory}: held by tmux session {session} since {since}; "
            "ask that manager to run crew.py release, or end that session"
        )


class WorkersError(Exception):
    @classmethod
    def name(cls, name: str):
        return cls(f"bad name {name!r}: use [A-Za-z0-9_-]+")

    @classmethod
    def override_syntax(cls, item: str):
        return cls(f"--env {item}: want NAME=VALUE")

    @classmethod
    def override_text(cls, key: str, value: str):
        return cls(f"--env {key}: {value!r}: {UNPRINTABLE}")

    @classmethod
    def note_length(cls, length: int, maximum: int):
        return cls(f"note: {length} characters: want at most {maximum}")

    @classmethod
    def note_text(cls, text: str):
        return cls(f"note: {text!r}: {UNPRINTABLE}")

    @classmethod
    def record_failed(cls, path: str, cause: OSError):
        return cls(f"{path}: {cause.strerror or cause}; start undone")

    @classmethod
    def resume_sid(cls, sid: str):
        return cls(f"--resume {sid}: not a session id")

    @classmethod
    def cwd(cls, path: str):
        return cls(f"--cwd {path}: not a directory")

    @classmethod
    def events_replaced(cls, path: str):
        return cls(f"events file {path}: replaced")

    @classmethod
    def events_shrank(cls, path: str):
        return cls(f"events file {path}: shrank")

    @classmethod
    def events_io(cls, path: str, cause: OSError):
        return cls(f"events file {path}: {cause.strerror or cause}")

    @classmethod
    def missing_session(cls, name: str):
        return cls(f"{name}: no tmux session")

    @classmethod
    def missing_entry(cls, name: str):
        return cls(f"{name} not in roster")

    @classmethod
    def no_caller(cls):
        return cls("stop-all: not in tmux: run it in the manager's tmux session")

    @classmethod
    def live_session(cls, name: str, session: str):
        return cls(f"{name}: session {session} is live")

    @classmethod
    def no_directory(cls):
        return cls("no manager directory: run inside tmux or give --manager <name>")

    @classmethod
    def configuration(cls, cause: Exception):
        return cls(f"config: {cause}")

    @classmethod
    def kill_failed(cls, sessions: list[str]):
        return cls(f"stop-all: still live after tmux kill-session: {', '.join(sessions)}")


class NotAWorker(WorkersError):
    def __init__(self, name: str):
        super().__init__(f"{name}: not a worker")


class Refused(WorkersError):
    @classmethod
    def rotation_due(cls):
        return cls(ROTATE_MSG)


class CommandArgumentError(argparse.ArgumentTypeError):
    @classmethod
    def after(cls, value: str):
        return cls(f"{value!r}: use a line number or end")

    @classmethod
    def number(cls, value: str):
        return cls(f"{value!r}: use a non-negative integer")
