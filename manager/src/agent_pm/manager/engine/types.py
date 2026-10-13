"""The manager's types: crew.py's Spec, crew.py's and roster.py's errors."""

import argparse
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
    split: str | None = None
    split_from: str | None = None


@dataclass(frozen=True)
class Entry:
    kind: str
    sid: str | None
    cwd: str
    resume: list[str]
    note: str | None = None
    opener: str | None = None
    pane: str | None = None
    split: str | None = None
    split_from: str | None = None
    tui: str | None = None
    state: str = "working"
    started: str | None = None


UNPRINTABLE = "want printable text (no control character, U+2028 or U+2029)"
ROTATE_MSG = "rotate: run crew.py attach, then arm with its N and GEN"


class RosterError(Exception):
    pass


class Stale(RosterError):
    pass


class Held(RosterError):
    def __init__(self, directory: str, session: str, since: str):
        super().__init__(
            f"manager directory {directory}: held by tmux session {session} since {since}; "
            "ask that manager to run crew.py release, or end that session"
        )


class WorkersError(Exception):
    pass


class NotAWorker(WorkersError):
    def __init__(self, name: str):
        super().__init__(f"{name}: not a worker")


class Refused(WorkersError):
    pass


class CommandArgumentError(argparse.ArgumentTypeError):
    pass
