"""One bounded native account probe; direct execution prints the raw result or null."""

import json
import os
import selectors
import subprocess
import sys
import time


def probe(env: dict, timeout: float = 10) -> dict | None:
    if type(timeout) not in (int, float) or not 0 < timeout <= 10:
        return None
    process = None
    try:
        deadline, pending, read_bytes = time.monotonic() + timeout, bytearray(), 0
        process = subprocess.Popen(
            ("codex", "app-server"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            bufsize=0,
        )
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)

            def send(value):
                process.stdin.write(json.dumps(value).encode("utf-8") + b"\n")

            def response(identifier):
                nonlocal read_bytes
                while True:
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending[:] = rest
                        record = json.loads(line)
                        if (
                            isinstance(record, dict)
                            and type(record.get("id")) is int
                            and record["id"] == identifier
                        ):
                            result = record.get("result")
                            return (
                                result
                                if isinstance(result, dict) and "error" not in record
                                else None
                            )
                    left = deadline - time.monotonic()
                    if left <= 0 or not selector.select(left):
                        return None
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        return None
                    read_bytes += len(chunk)
                    if read_bytes > 1024 * 1024:
                        return None
                    pending.extend(chunk)

            send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "agent_pm", "version": "0.1.0"}},
                }
            )
            if response(1) is None:
                return None
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read"})
            return response(2)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


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
