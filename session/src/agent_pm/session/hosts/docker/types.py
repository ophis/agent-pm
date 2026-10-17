"""The broker's start response, as the Docker Host reads it."""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Started:
    container: str
    endpoint: str
    image_digest: str
    clients: Mapping[str, str]  # client name → its runnable version
