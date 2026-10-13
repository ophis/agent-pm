"""Parsed host settings."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Hosts:
    default: str
    tables: dict[str, dict]
