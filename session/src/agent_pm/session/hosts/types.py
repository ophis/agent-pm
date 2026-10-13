"""Parsed host settings and their configuration failures."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Hosts:
    default: str
    tables: dict[str, dict]


class HostsError(ValueError):
    @classmethod
    def missing(cls) -> "HostsError":
        return cls("[hosts]: missing")

    @classmethod
    def key_type(cls, key) -> "HostsError":
        return cls(f"[hosts] {key!r}: want a string key")

    @classmethod
    def table(cls, key) -> "HostsError":
        return cls(f"[hosts] {key}: want default or a [hosts.<name>] table")

    @classmethod
    def name(cls, key) -> "HostsError":
        return cls(f"[hosts.{key}]: want a name matching [a-z][a-z0-9_]*")

    @classmethod
    def implementation(cls, key) -> "HostsError":
        return cls(f"[hosts.{key}].implementation: want module.path:Class")

    @classmethod
    def default(cls, choices) -> "HostsError":
        return cls(f"[hosts] default: want one of {', '.join(sorted(choices))}")

    @classmethod
    def session_table(cls) -> "HostsError":
        return cls("session config: want a table")
