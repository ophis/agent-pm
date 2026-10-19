"""The selector's data types: its config, decision API backends and failure codes."""

from dataclasses import dataclass, field
from enum import StrEnum


class Backend(StrEnum):
    """The decision API, both System One compatible: Jev (hosted) or Kev (self-hosted)."""

    JEV = "jev"
    KEV = "kev"


class Failure(StrEnum):
    ERROR = "error"
    TIMEOUT = "timeout"
    NO_KEY = "no-key"
    INVALID = "invalid"  # a malformed reply, or an answer not among the choices


class SelectorError(Exception):
    """A failed decision call; its str() is the code only, never a key, state or reply."""

    def __init__(self, reason: Failure):
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class SelectorConfig:
    """What a decision API Selector calls: url https, or http only on a loopback host; Jev's
    defaults: https://api.typesafe.ai/v1/systemone, jev-1.13.0, 2 s, key from
    /usr/bin/security find-generic-password -s typesafe-jev -w; Kev's url and model have none.
    """

    backend: Backend
    url: str
    model: str
    timeout: float  # seconds
    key_command: tuple[str, ...] = field(default=(), repr=False)  # argv, no shell; () → no key
