"""Caller-owned adapter settings shared by Registry and launch strategies."""

from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class TuiSettings:
    """tmux's status line, a grid's panes per column, when it re-tiles (None: tmux_host's)."""

    status_line: bool = False
    per_column: int | None = None
    retile: str | None = None


@dataclass(frozen=True)
class Settings:
    adapters: dict[str, dict]
    hosts: dict[str, dict]
    default_host: str
