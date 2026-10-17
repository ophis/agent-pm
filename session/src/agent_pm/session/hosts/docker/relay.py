"""The container's hook relay (stdlib only, run by path): listens on SOCKET; per connection, in
order, it forwards one request line (at most LINE_MAX bytes) to stdout and sends back the first ack
line from stdin carrying that request's id within ACK_WAIT seconds, dropping acks for other ids.
stdin's EOF removes the socket and ends it.
"""

import contextlib
import json
import os
import select
import socket
import sys
import time

SOCKET = "/run/agent-pm/relay.sock"
LINE_MAX = 90 << 20
ACK_WAIT = 10


def request_id(line: bytes):
    try:
        found = json.loads(line)
    except (ValueError, RecursionError):
        return None
    value = found.get("id") if isinstance(found, dict) else None
    return value if type(value) is int else None


class Acks:
    def __init__(self, fd: int) -> None:
        self.fd, self.buffer, self.ended = fd, b"", False

    def line(self, timeout: float | None) -> bytes | None:
        """The next line; None at the deadline (None: none) or EOF."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self.buffer:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            if self.ended or not select.select([self.fd], [], [], left)[0]:
                return None
            chunk = os.read(self.fd, 65536)
            self.ended = not chunk
            self.buffer = self.buffer[-LINE_MAX:] + chunk
        line, _, self.buffer = self.buffer.partition(b"\n")
        return line + b"\n"


def serve(conn: socket.socket, requests, acks: Acks, wait: float = ACK_WAIT) -> None:
    conn.settimeout(wait)
    with conn.makefile("rb") as reader:
        line = reader.readline(LINE_MAX + 1)
    if not line.endswith(b"\n"):
        return
    wanted = request_id(line)
    requests.write(line)
    requests.flush()
    deadline = time.monotonic() + wait
    while (ack := acks.line(max(0.0, deadline - time.monotonic()))) is not None:
        if request_id(ack) == wanted:
            conn.sendall(ack)
            return


def main(path: str = SOCKET) -> int:
    acks = Acks(sys.stdin.fileno())
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(path)
        listener.listen()
        while not acks.ended:
            ready = select.select([listener, acks.fd], [], [])[0]
            if acks.fd in ready:
                acks.line(0)  # a stale ack, or EOF
            if listener in ready:
                conn, _ = listener.accept()
                with conn, contextlib.suppress(OSError):
                    serve(conn, sys.stdout.buffer, acks)
    finally:
        listener.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
