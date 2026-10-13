"""Crew launch intent, roster entries, states and errors."""

from dataclasses import dataclass
from enum import StrEnum

from agent_pm.utils import Record


@dataclass(frozen=True)
class Spec:
    name: str
    argv: tuple[str, ...]
    note: str | None = None


class State(StrEnum):
    STARTING = "starting"
    LIVE = "live"
    DONE = "done"
    WAITING = "waiting"
    GONE = "gone"
    LOST = "lost"
    RESUMING = "resuming"


@dataclass(frozen=True)
class Entry:
    record: Record | None
    state: State
    note: str | None
    started: str
    argv: tuple[str, ...] = ()


class RosterError(Exception):
    pass


class Stale(RosterError):
    pass


class Held(RosterError):
    pass
