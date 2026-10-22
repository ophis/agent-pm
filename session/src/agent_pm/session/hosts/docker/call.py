"""Synchronous, fixed host command over a container socket and a dedicated exec stream.

Run by path with the standard library only: ``call``, ``serve GENERATION``, or
``host CONFIG_PATH CONNECTOR_JSON``. Only the host config determines the command.
"""

import contextlib
import errno
import fcntl
import json
import os
import re
import select
import socket
import stat
import subprocess
import sys
import tempfile
import time

SOCKET = "/run/agent-pm/call.sock"
OUTPUT_MAX = 64 << 10
LINE_MAX = OUTPUT_MAX * 6 + 256
REQUEST_MAX = 256
WAIT = 180
FAILURE = "Host call failed.\n"
GENERATION = re.compile(r"[0-9a-f]{32}")


def _unique(pairs):
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("duplicate key")
    return found


def _constant(_value):
    raise ValueError("invalid constant")


def _object(line: bytes, limit: int):
    if len(line) > limit or not line.endswith(b"\n"):
        return None
    try:
        found = json.loads(line, object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return found if isinstance(found, dict) else None


def _generation(value) -> bool:
    return isinstance(value, str) and GENERATION.fullmatch(value) is not None


def request(line: bytes) -> dict | None:
    found = _object(line, REQUEST_MAX)
    return found if found == {} else None


def host_request(line: bytes, generation: str) -> bool:
    return _object(line, REQUEST_MAX) == {"generation": generation}


def reply(line: bytes) -> dict | None:
    found = _object(line, LINE_MAX)
    if not (
        found is not None
        and found.keys() == {"returncode", "stdout"}
        and type(found["returncode"]) is int
        and 0 <= found["returncode"] <= 255
        and isinstance(found["stdout"], str)
    ):
        return None
    try:
        return found if len(found["stdout"].encode("utf-8")) <= OUTPUT_MAX else None
    except UnicodeError:
        return None


def configuration(found) -> dict | None:
    if not (
        isinstance(found, dict)
        and found.keys() == {"generation", "argv", "env"}
        and _generation(found["generation"])
        and isinstance(found["argv"], list)
        and found["argv"]
        and all(isinstance(arg, str) and "\0" not in arg for arg in found["argv"])
        and os.path.isabs(found["argv"][0])
        and isinstance(found["env"], dict)
        and found["env"].keys() == {"AGENT_PM_CLIENT"}
        and all(isinstance(value, str) and "\0" not in value for value in found["env"].values())
    ):
        return None
    return found


def failed() -> dict:
    return {"returncode": 2, "stdout": FAILURE}


def _wire(found: dict) -> bytes:
    return json.dumps(found, separators=(",", ":")).encode("utf-8") + b"\n"


def _send(stream, found: dict) -> None:
    stream.write(_wire(found))
    stream.flush()


class _Reader:
    """Bounded lines with a deadline, including partially written pipe messages."""

    def __init__(self, stream):
        self.stream, self.pending = stream, b""

    def line(self, limit: int, timeout: float | None = None) -> bytes:
        deadline = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self.pending:
            if len(self.pending) > limit:
                raise ValueError("oversized message")
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.stream], [], [], remaining)[0]:
                    raise TimeoutError
            chunk = self.stream.read1(min(4096, limit + 1 - len(self.pending)))
            if not chunk:
                found, self.pending = self.pending, b""
                return found
            self.pending += chunk
        found, self.pending = self.pending.split(b"\n", 1)
        if len(found) + 1 > limit:
            raise ValueError("oversized message")
        return found + b"\n"


def read_socket(connection, limit: int = LINE_MAX, *, deadline=None) -> bytes:
    deadline = time.monotonic() + WAIT if deadline is None else deadline
    found = b""
    while b"\n" not in found:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        connection.settimeout(remaining)
        chunk = connection.recv(min(4096, limit + 1 - len(found)))
        if not chunk:
            return found
        found += chunk
        if len(found) > limit:
            raise ValueError("oversized message")
    return found


def execute(config: dict, *, run=None) -> dict:
    """Execute only the preauthorized host command; never return host diagnostics."""
    run = subprocess.run if run is None else run
    try:
        with tempfile.TemporaryFile() as output:
            result = run(
                config["argv"],
                cwd="/",
                env={**os.environ, **config["env"]},
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=150,
            )
            output.seek(0)
            data = output.read(OUTPUT_MAX + 1)
        code = result.returncode
        if type(code) is not int:
            return failed()
        code = 128 - code if code < 0 else code
        if len(data) > OUTPUT_MAX or not 0 <= code <= 255:
            return failed()
        return {"returncode": code, "stdout": data.decode("utf-8")}
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return failed()


def client(*, stdout=None, stderr=None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        deadline = time.monotonic() + WAIT
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(WAIT)
            connection.connect(SOCKET)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection.settimeout(remaining)
            connection.sendall(b"{}\n")
            found = reply(read_socket(connection, deadline=deadline))
            if found is None:
                raise ValueError("invalid reply")
        stdout.write(found["stdout"])
        stdout.flush()
        return found["returncode"]
    except (OSError, ValueError):
        stderr.write(FAILURE)
        stderr.flush()
        return 2


@contextlib.contextmanager
def listener(path: str):
    """Keep live services intact; reclaim a stale socket only after a failed probe."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    with open(path + ".lock", "a", encoding="ascii") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            try:
                server.bind(path)
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
                before = os.lstat(path)
                if not stat.S_ISSOCK(before.st_mode):
                    raise
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(1)
                    try:
                        probe.connect(path)
                    except OSError as probe_error:
                        if probe_error.errno != errno.ECONNREFUSED:
                            raise
                    else:
                        raise OSError(errno.EADDRINUSE, "listener active") from None
                if os.lstat(path) != before:
                    raise OSError(errno.EADDRINUSE, "listener changed") from None
                os.unlink(path)
                server.bind(path)
            owned = os.lstat(path)
            try:
                os.chmod(path, 0o600)
                server.listen(8)
                yield server
            finally:
                with contextlib.suppress(FileNotFoundError):
                    current = os.lstat(path)
                    if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                        os.unlink(path)


def serve(
    generation: str, *, stdin=None, stdout=None, path: str = SOCKET, listener_factory=None
) -> int:
    if not _generation(generation):
        return 2
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    listener_factory = listener if listener_factory is None else listener_factory
    reader = _Reader(stdin)
    try:
        with listener_factory(path) as server:
            _send(stdout, {"ready": True, "generation": generation})
            while True:
                readable = select.select([server, stdin], [], [])[0]
                if stdin in readable:
                    return 2
                connection, _address = server.accept()
                with connection:
                    try:
                        incoming = read_socket(connection, REQUEST_MAX)
                    except (OSError, ValueError):
                        with contextlib.suppress(OSError):
                            connection.sendall(_wire(failed()))
                        continue
                    if request(incoming) is None:
                        with contextlib.suppress(OSError):
                            connection.sendall(_wire(failed()))
                        continue
                    _send(stdout, {"generation": generation})
                    found = reply(reader.line(LINE_MAX, WAIT))
                    with contextlib.suppress(OSError):
                        connection.sendall(_wire(failed() if found is None else found))
                    if found is None:
                        return 2
    except (OSError, ValueError):
        return 2


def _stop(process) -> None:
    try:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _await_ready(path: str, handshake: dict) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with open(path, "rb") as stream:
                if _object(stream.read(REQUEST_MAX + 1), REQUEST_MAX) == handshake:
                    return True
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    return False


def host(config_path: str, connector: list[str], *, stdout=None, popen=None) -> int:
    stdout = sys.stdout.buffer if stdout is None else stdout
    popen = subprocess.Popen if popen is None else popen
    if not (
        isinstance(connector, list)
        and connector
        and all(isinstance(arg, str) and "\0" not in arg for arg in connector)
    ):
        return 2
    try:
        with open(config_path, "rb") as stream:
            config = configuration(
                json.load(stream, object_pairs_hook=_unique, parse_constant=_constant)
            )
        if config is None:
            return 2
        handshake = {"ready": True, "generation": config["generation"]}
        ready_path = config_path + ".ready"
        with open(config_path + ".lock", "a", encoding="ascii") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if _await_ready(ready_path, handshake):
                    _send(stdout, handshake)
                    return 0
                return 2
            with contextlib.suppress(FileNotFoundError):
                os.unlink(ready_path)
            process = popen(
                connector, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            try:
                reader = _Reader(process.stdout)
                if _object(reader.line(REQUEST_MAX, 10), REQUEST_MAX) != handshake:
                    return 2
                with open(ready_path, "wb") as stream:
                    _send(stream, handshake)
                _send(stdout, handshake)
                while True:
                    incoming = reader.line(REQUEST_MAX)
                    if not incoming:
                        return 0
                    found = (
                        execute(config)
                        if host_request(incoming, config["generation"])
                        else failed()
                    )
                    _send(process.stdin, found)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(ready_path)
                _stop(process)
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
        return 2


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["call"]:
        return client()
    try:
        if len(argv) == 2 and argv[0] == "serve":
            result = serve(argv[1])
        elif len(argv) == 3 and argv[0] == "host":
            result = host(argv[1], json.loads(argv[2]))
        else:
            result = 2
    except (OSError, ValueError, RecursionError):
        result = 2
    if result == 2:
        sys.stderr.write(FAILURE)
        sys.stderr.flush()
    return result


if __name__ == "__main__":
    sys.exit(main())
