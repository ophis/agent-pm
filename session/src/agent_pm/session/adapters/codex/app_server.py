"""The Codex app-server JSON-RPC client, stdlib only (tui_start.py loads it by path). Run by path,
it is the account probe: prints the account/rateLimits/read result, or null on any failure
(--timeout S, 0 < S <= 10).
"""

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


class AppServer:
    """Own one app-server process and close its group after a single initialized request."""

    def __init__(
        self,
        exe: str,
        args: list[str],
        *,
        env: dict[str, str],
        wait: float,
        limit: int,
        cwd: str | None = None,
        termination: Termination | None = None,
        exit_wait: float = EXIT_WAIT,
    ):
        self.exe, self.args, self.env, self.cwd = exe, args, env, cwd
        self.wait, self.limit = wait, limit
        self.termination, self.exit_wait = termination, exit_wait

    def call(self, method: str, params: dict | None = None):
        """Initialize and return `method` within the byte/time bounds, then reap the process."""
        started = exited = False
        try:
            if self.termination is not None:
                self.termination.check()
            try:
                self._proc = subprocess.Popen(
                    [self.exe, "app-server", *self.args],
                    cwd=self.cwd,
                    env=self.env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as e:
                raise Error(f"{self.exe}: {e.strerror}") from e
            started = True
            assert self._proc.stdin is not None and self._proc.stdout is not None
            self._stdin, self._stdout = self._proc.stdin, self._proc.stdout
            if self.termination is not None:
                self.termination.check()
            self._lines = Lines(
                self._stdout.fileno(), self.wait, self.limit, termination=self.termination
            )
            self._send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "agent-pm", "version": "0"},
                        "capabilities": {"experimentalApi": True},
                    },
                }
            )
            self._answer(1, "initialize")
            self._send({"method": "initialized"})
            self._send(
                {"id": 2, "method": method, **({"params": params} if params is not None else {})}
            )
            return self._answer(2, method)
        except Exited:
            exited = True
            raise
        finally:
            if started:
                self._close()
            if self.termination is not None:
                self.termination.check()
            if exited and started:
                raise Exited(self._proc.returncode)

    def _send(self, message: dict) -> None:
        try:
            self._stdin.write(json.dumps(message).encode() + b"\n")
            self._stdin.flush()
        except BrokenPipeError:
            raise Exited from None

    def _answer(self, ident: int, method: str):
        while True:
            try:
                message = json.loads(next(self._lines))
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

    def _close(self) -> None:
        deadline = time.monotonic() + self.exit_wait
        with contextlib.suppress(OSError):
            self._stdin.close()
        fd = self._stdout.fileno()
        try:
            for interval in _polls(deadline, self.termination):
                if select.select([fd], [], [], interval)[0]:
                    if not os.read(fd, BLOCK):
                        break
                elif self._proc.poll() is not None:
                    break
            for interval in _polls(deadline, self.termination):
                try:
                    self._proc.wait(timeout=interval)
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self._proc.pid, signal.SIGKILL)
            self._proc.wait()
            self._stdout.close()


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


def _polls(deadline: float, termination):
    while (left := deadline - time.monotonic()) > 0:
        if termination is not None and termination.signum:
            return
        yield min(left, POLL)


def probe(env: dict, timeout: float = 10) -> dict | None:
    if type(timeout) not in (int, float) or not 0 < timeout <= 10:
        return None
    try:
        result = AppServer(
            "codex",
            [],
            env=env,
            wait=timeout,
            limit=PROBE_MAX,
            exit_wait=POLL,
        ).call("account/rateLimits/read")
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
