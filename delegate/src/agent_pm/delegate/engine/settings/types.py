"""Malformed Delegate settings retain ValueError catches and distinguish incorrect value types."""

from collections.abc import Collection


class SettingsError(ValueError):
    @classmethod
    def harness_key(cls, key):
        return cls(f"[delegates] {key}: the delegate takes only harness_host")

    @classmethod
    def harness_host(cls, hosts: Collection[str]):
        return cls(f"[delegates] harness_host: want one of {', '.join(sorted(hosts))}")

    @classmethod
    def policy_keys(cls, runtime: str):
        return cls(f"policy.{runtime} must contain only flags/env")


class SettingsTypeError(TypeError, SettingsError):
    @classmethod
    def delegates(cls):
        return cls("[delegates]: the delegate takes only harness_host")

    @classmethod
    def policy(cls):
        return cls("policy must be a table")

    @classmethod
    def runtime(cls, runtime: str):
        return cls(f"policy.{runtime} must contain only flags/env")

    @classmethod
    def flags(cls, runtime: str):
        return cls(f"policy.{runtime}.flags must be a list of strings without NUL")
