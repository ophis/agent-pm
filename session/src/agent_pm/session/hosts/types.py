"""Parsed host settings and their configuration failures."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Hosts:
    default: str
    tables: dict[str, dict]


class HostsError(ValueError):
    pass
