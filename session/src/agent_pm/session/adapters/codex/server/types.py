"""Codex RPC transport options and expected server failures for isolated helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class Termination(Protocol):
    signum: int

    def check(self) -> None: ...


@dataclass(frozen=True, kw_only=True)
class ProcessRequest:
    exe: str
    args: list[str]
    cwd: str
    env: dict[str, str]


@dataclass(frozen=True, kw_only=True)
class RpcOptions:
    env: dict[str, str]
    wait: float
    limit: int
    cwd: str | None = None
    termination: Termination | None = None
    exit_wait: float = 5


class Error(Exception):
    @classmethod
    def unavailable(cls, exe: str, cause: OSError) -> Error:
        return cls(f"{exe}: {cause.strerror}")

    @classmethod
    def timeout(cls, seconds: float) -> Error:
        return cls(f"no answer in {seconds} s")

    @classmethod
    def oversized_answer(cls, limit: int) -> Error:
        return cls(f"an answer over {limit} bytes")

    @classmethod
    def failed_answer(cls, method: str, error: object) -> Error:
        detail = error.get("message") if isinstance(error, dict) else error
        return cls(f"{method} failed: {detail}")


class Exited(Exception):
    pass
