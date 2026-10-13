"""Standalone standard-library Codex app-server JSON-RPC client and account probe."""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from typing import Protocol


class Termination(Protocol):
    signum: int

    def check(self) -> None: ...


class Error(Exception):
    pass


class Exited(Exception):
    pass


POLL = 0.1

BLOCK = 1 << 16

EXIT_WAIT = 5

PROBE_MAX = 1 << 20


def call(
    exe: str,
    args: list[str],
    method: str,
    params: dict | None = None,
    *,
    env: dict[str, str],
    wait: float,
    limit: int,
    cwd: str | None = None,
    termination: Termination | None = None,
    exit_wait: float = EXIT_WAIT,
):
    """`exe app-server *args` in its own process group: initialize, then `method`'s result within
    `wait` s and `limit` buffered bytes; else Error, or Exited (its returncode) if it ends first.
    Its group is killed before returning.
    """
    proc = None
    exited = False
    try:
        if termination is not None:
            termination.check()
        try:
            proc = subprocess.Popen(
                [exe, "app-server", *args],
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            raise Error(f"{exe}: {e.strerror}") from e
        if termination is not None:
            termination.check()
        assert proc.stdout is not None
        lines = Lines(proc.stdout.fileno(), wait, limit, termination=termination)
        send(
            proc,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-pm", "version": "0"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        answer(lines, 1, "initialize")
        send(proc, {"method": "initialized"})
        send(
            proc, {"id": 2, "method": method, **({"params": params} if params is not None else {})}
        )
        return answer(lines, 2, method)
    except Exited:
        exited = True
        raise
    finally:
        if proc is not None:
            close(proc, termination, exit_wait)
        if termination is not None:
            termination.check()
        if exited and proc is not None:
            raise Exited(proc.returncode)


def send(proc, message: dict) -> None:
    try:
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()
    except BrokenPipeError:
        raise Exited from None


class Lines:
    def __init__(self, fd: int, wait: float, limit: int, *, termination: Termination | None = None):
        self.fd, self.wait, self.limit, self.termination = fd, wait, limit, termination
        self.deadline, self.buf, self.seen = time.monotonic() + wait, bytearray(), 0

    def __next__(self) -> bytes:
        while (end := self.buf.find(b"\n", self.seen)) < 0:
            if self.termination is not None:
                self.termination.check()
            self.seen = len(self.buf)
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise Error(f"no answer in {self.wait} s")
            if not select.select([self.fd], [], [], min(left, POLL))[0]:
                continue
            chunk = os.read(self.fd, BLOCK)
            if not chunk:
                raise Exited
            if len(self.buf) + len(chunk) > self.limit:
                raise Error(f"an answer over {self.limit} bytes")
            self.buf += chunk
        line = bytes(self.buf[:end])
        del self.buf[: end + 1]
        self.seen = 0
        return line


def answer(lines, ident: int, method: str):
    while True:
        try:
            message = json.loads(next(lines))
        except (ValueError, RecursionError):
            continue
        if (
            not isinstance(message, dict)
            or "method" in message
            or type(message.get("id")) is not int
            or message["id"] != ident
        ):
            continue
        if "result" in message:
            return message["result"]
        error = message.get("error")
        detail = error.get("message") if isinstance(error, dict) else error
        raise Error(f"{method} failed: {detail}")


def _polls(deadline: float, termination):
    while (left := deadline - time.monotonic()) > 0:
        if termination is not None and termination.signum:
            return
        yield min(left, POLL)


def close(proc, termination=None, wait: float = EXIT_WAIT) -> None:
    deadline = time.monotonic() + wait
    with contextlib.suppress(OSError):
        proc.stdin.close()
    fd = proc.stdout.fileno()
    try:
        for interval in _polls(deadline, termination):
            if select.select([fd], [], [], interval)[0]:
                if not os.read(fd, BLOCK):
                    break
            elif proc.poll() is not None:
                break
        for interval in _polls(deadline, termination):
            try:
                proc.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        proc.stdout.close()


def probe(env: dict, timeout: float = 10) -> dict | None:
    if type(timeout) not in (int, float) or not 0 < timeout <= 10:
        return None
    try:
        result = call(
            "codex",
            [],
            "account/rateLimits/read",
            env=env,
            wait=timeout,
            limit=PROBE_MAX,
            exit_wait=POLL,
        )
    except (Error, Exited, OSError, ValueError):
        return None
    return result if isinstance(result, dict) else None


def main():
    argv = sys.argv[1:]
    try:
        timeout = (
            10 if not argv else float(argv[1]) if len(argv) == 2 and argv[0] == "--timeout" else 0
        )
        output = json.dumps(probe(dict(os.environ), timeout), allow_nan=False)
    except (ValueError, RecursionError):
        output = "null"
    print(output)


if __name__ == "__main__":
    main()
