"""The selector's data types: its config, decision API backends and failure codes."""

from dataclasses import dataclass, field
from enum import StrEnum


class Backend(StrEnum):
    JEV = "jev"
    KEV = "kev"


class Failure(StrEnum):
    ERROR = "error"
    TIMEOUT = "timeout"
    NO_KEY = "no-key"
    INVALID = "invalid"


class SelectorError(Exception):
    """str() is the code only."""

    def __init__(self, reason: Failure):
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class SelectorConfig:
    """url: https, or http on a loopback host."""

    backend: Backend
    url: str
    model: str
    timeout: float  # seconds
    key_command: tuple[str, ...] = field(default=(), repr=False)  # argv; () → no key
