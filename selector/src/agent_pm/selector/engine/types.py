"""The selector's data types: state, question, config, backends and failure codes."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class Backend(StrEnum):
    JEV = "jev"
    KEV = "kev"


class Failure(StrEnum):
    ERROR = "error"
    TIMEOUT = "timeout"
    NO_KEY = "no-key"
    INVALID = "invalid"
    TOO_LARGE = "too-large"


class SelectorError(Exception):
    """str() is the code only."""

    def __init__(self, reason: Failure):
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class State:
    """JSON values: fields, always sent; droppable (key, items oldest first), oldest dropped to
    fit. ValueError: a blank droppable key, one in fields, or a non-JSON value.
    """

    fields: Mapping[str, object]
    droppable: tuple[str, tuple[object, ...]] | None = None

    def __post_init__(self):
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        if self.droppable is not None:
            key, items = self.droppable
            if not isinstance(key, str) or not key.strip():
                raise ValueError("state: want a non-blank droppable key")
            if key in self.fields:
                raise ValueError("state: want a droppable key that is not a field")
            object.__setattr__(self, "droppable", (key, tuple(items)))
        try:
            json.dumps(self.sent(), allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("state: want JSON values") from None

    def sent(self, dropped: int = 0) -> dict[str, object]:
        """Without the oldest `dropped` droppable items."""
        if self.droppable is None:
            return dict(self.fields)
        key, items = self.droppable
        return {**self.fields, key: list(items[dropped:])}


@dataclass(frozen=True)
class Question:
    """options: option → description, in order. ValueError: none, a blank one, blank instructions."""

    instructions: str
    options: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError("question: want non-blank instructions")
        if not self.options or any(not isinstance(o, str) or not o.strip() for o in self.options):
            raise ValueError("question: want one or more non-blank options")


@dataclass(frozen=True)
class SelectorConfig:
    """url: https, or http on a loopback host."""

    backend: Backend
    url: str
    model: str
    timeout: float  # seconds
    window: int  # tokens per question
    key_command: tuple[str, ...] = field(default=(), repr=False)  # argv; () → no key
