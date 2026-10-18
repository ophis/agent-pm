"""Runs COMMAND, `opencode run`, with TEXT on its stdin; copies its stdout lines and its stderr, and
kills it, exiting 124, when none of its stdout lines is a JSON object with a string sessionID
within SECONDS. A COMMAND it cannot start: 127; bad arguments: 2. Standalone: the standard
library only.
"""

import json
import os
import subprocess
import sys
import threading

USAGE = "usage: launcher.py --timeout SECONDS --prompt TEXT -- COMMAND..."
STDERR_WAIT = 2  # seconds to copy COMMAND's last stderr after it exits


def _arguments(argv: list[str]) -> tuple[float, str, list[str]]:
    if len(argv) < 6 or argv[0] != "--timeout" or argv[2] != "--prompt" or argv[4] != "--":
        raise ValueError
    seconds = float(argv[1])
    if not 0 < seconds < float("inf"):
        raise ValueError
    return seconds, argv[3], argv[5:]


def _bound(line: bytes) -> bool:
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):
        return False
    return isinstance(record, dict) and isinstance(record.get("sessionID"), str)


def _write(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]


def _feed(stream, text: str) -> None:
    try:
        with stream:
            stream.write(text.encode())
    except OSError:
        pass


def _copy(fd: int) -> None:
    try:
        while chunk := os.read(fd, 65536):
            _write(sys.stderr.fileno(), chunk)
    except OSError:
        pass


def main(argv: list[str]) -> int:
    try:
        seconds, text, command = _arguments(argv)
    except ValueError:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        child = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except FileNotFoundError:
        print(f"opencode: command not found: {command[0]}", file=sys.stderr)
        return 127
    except OSError as error:
        print(f"opencode: cannot run {command[0]}: {error.strerror}", file=sys.stderr)
        return 127
    assert child.stdin is not None and child.stdout is not None and child.stderr is not None
    session = threading.Event()

    def expire() -> None:
        if not session.is_set() and child.poll() is None:
            child.kill()
            _write(
                sys.stderr.fileno(), f"opencode: no session after {argv[1]} s; stopped\n".encode()
            )
            os._exit(124)

    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    copier = threading.Thread(target=_copy, args=(child.stderr.fileno(),), daemon=True)
    for thread in (
        threading.Thread(target=_feed, args=(child.stdin, text), daemon=True),
        copier,
        timer,
    ):
        thread.start()
    try:
        for line in child.stdout:
            if not session.is_set() and _bound(line):
                session.set()
            _write(sys.stdout.fileno(), line)
    except OSError:
        child.kill()
    code = child.wait()
    timer.cancel()
    copier.join(STDERR_WAIT)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
