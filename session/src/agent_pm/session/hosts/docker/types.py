"""The broker's start response, as the Docker Host reads it."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Started:
    container: str
    endpoint: str
    image_digest: str
    clients: Mapping[str, str]  # client name → its runnable version
    github: Sequence[Mapping[str, str]]  # each GitHub token's {"account", "expires_at"}
