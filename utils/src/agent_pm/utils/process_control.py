"""Generation-scoped process control over a private Unix socket. A controller never reaps its
anchor until that process group has no live members, so its numeric identity cannot be reused.
The standalone entry point runs prepare()'s command with inherited terminal, environment and mask.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence

POLL = 0.05
TIMEOUT = 10


def _write(path, value):
    temporary = path + ".tmp"
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
        json.dump(value, stream)
    os.replace(temporary, path)


def _read(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None


class _Group:
    def __init__(self, pid: int, channel: int):
        self.pid: int | None = pid
        self.channel = channel
        self.pending = b""
        self.returncode: int | None = None
        self.eof = False
        os.set_blocking(channel, False)

    @classmethod
    def launch(cls, argv, cwd, **options):
        read, write = os.pipe()
        try:
            pid = os.fork()
        except BaseException:
            os.close(read)
            os.close(write)
            raise
        if pid == 0:
            os.close(read)
            cls._anchor(write, argv, cwd, **options)
        os.close(write)
        os.setpgid(pid, pid)
        return cls(pid, read)

    @staticmethod
    def _anchor(channel, argv, cwd, **options):
        try:
            os.setpgid(0, 0)
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, lambda *_: None)
            if os.isatty(0):
                prior = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                os.tcsetpgrp(0, os.getpid())
                signal.signal(signal.SIGTTOU, prior)
            program = subprocess.Popen(argv, cwd=cwd, **options)
            os.write(channel, b'{"started": true}\n')
            rc = program.wait()
            os.write(channel, json.dumps({"rc": rc}).encode() + b"\n")
        except (OSError, ValueError) as error:
            message = {
                "error": error.strerror if isinstance(error, OSError) else str(error),
                "errno": error.errno if isinstance(error, OSError) else errno.EINVAL,
                "filename": error.filename if isinstance(error, OSError) else None,
            }
            os.write(channel, json.dumps(message).encode() + b"\n")
        finally:
            os._exit(0)

    def receive(self) -> list[dict]:
        with contextlib.suppress(BlockingIOError):
            data = os.read(self.channel, 65536)
            self.eof = not data
            self.pending += data
        messages = []
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            message = json.loads(line)
            if "rc" in message:
                self.returncode = message["rc"]
            elif "error" in message:
                self.returncode = 1
            messages.append(message)
        return messages

    def live(self) -> bool:
        if self.pid is None:
            return False
        self.receive()
        if self.returncode is None and not self.eof:
            return True
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=,pgid=,stat="],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": os.defpath, "LC_ALL": "C"},
        )
        if result.returncode:
            raise OSError(f"cannot inspect process group: {result.stderr.strip()}")
        for line in result.stdout.splitlines():
            _, group, state = line.split()
            if int(group) == self.pid and not state.startswith("Z"):
                return True
        self._reap()
        return False

    def send(self, sig: int) -> None:
        if self.live() and self.pid is not None:
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                self._reap()

    def kill(self) -> None:
        self.send(signal.SIGKILL)

    def _reap(self) -> None:
        if self.pid is not None:
            _, status = os.waitpid(self.pid, 0)
            self.pid = None
            self.receive()
            if self.returncode is None:
                self.returncode = os.waitstatus_to_exitcode(status)


class ProcessControl:
    def __init__(self, path: str):
        self.path = path
        self.ended = False

    @classmethod
    def prepare(cls, argv: Sequence[str], cwd: str) -> ProcessControl:
        path = tempfile.mkdtemp(prefix="apm-group-", dir="/tmp")
        try:
            _write(os.path.join(path, "launch.json"), {"argv": list(argv), "cwd": cwd})
        except BaseException:
            shutil.rmtree(path)
            raise
        return cls(path)

    def command(self) -> list[str]:
        return [sys.executable, "-I", os.path.abspath(__file__), self.path]

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def wait_started(self, timeout: float = TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while (message := _read(os.path.join(self.path, "started.json"))) is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("process controller did not start")
            time.sleep(POLL)
        if "error" in message:
            raise OSError(message["errno"], message["error"], message.get("filename"))

    def running(self) -> bool:
        return (
            not self.ended
            and os.path.isdir(self.path)
            and all(
                _read(os.path.join(self.path, name)) is None
                for name in ("exited.json", "done.json")
            )
        )

    def live(self) -> bool:
        return self._request("live")

    def kill(self) -> None:
        self._request("kill")

    def _request(self, command: str) -> bool:
        if (
            self.ended
            or not os.path.isdir(self.path)
            or _read(os.path.join(self.path, "done.json")) is not None
        ):
            self.ended = True
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(TIMEOUT)
                connection.connect(os.path.join(self.path, "control"))
                connection.sendall(command.encode() + b"\n")
                with connection.makefile("rb") as stream:
                    line = stream.readline()
                if not line:
                    raise ConnectionResetError("process controller disconnected")
                response = json.loads(line)
        except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError):
            if (
                not os.path.isdir(self.path)
                or _read(os.path.join(self.path, "done.json")) is not None
            ):
                self.ended = True
                return False
            raise
        if "error" in response:
            raise OSError(response["errno"], response["error"])
        self.ended = not response["live"]
        return response["live"]

    def serve(self, *, on_exit: Callable[[int], None] | None = None, **options) -> int:
        launch = _read(os.path.join(self.path, "launch.json"))
        if launch is None:
            raise FileNotFoundError("missing process launch")
        os.unlink(os.path.join(self.path, "launch.json"))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(os.path.join(self.path, "control"))
            listener.listen()
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            group = _Group.launch(launch["argv"], launch["cwd"], **options)
            try:
                return self._serve(listener, group, on_exit)
            finally:
                os.close(group.channel)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.path, "control"))

    def _serve(self, listener, group, on_exit) -> int:
        stopping: int | None = None

        def stop(signum, _frame):
            nonlocal stopping
            stopping = signum

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, stop)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
        started, reported = False, False
        while True:
            for message in group.receive():
                if "started" in message or "error" in message:
                    _write(os.path.join(self.path, "started.json"), message)
                    started = True
            if group.eof and not started:
                _write(
                    os.path.join(self.path, "started.json"),
                    {"error": "anchor exited before launch", "errno": errno.ECHILD},
                )
                started = True
            if group.returncode is not None and not reported:
                _write(os.path.join(self.path, "exited.json"), {"rc": group.returncode})
                if on_exit is not None:
                    on_exit(group.returncode)
                reported = True
            try:
                if stopping is not None:
                    group.send(stopping)
                    stopping = None
                live = not started or group.live()
            except OSError:
                live = True
            if started and not live:
                rc = group.returncode if group.returncode is not None else 1
                if not reported:
                    _write(os.path.join(self.path, "exited.json"), {"rc": rc})
                    if on_exit is not None:
                        on_exit(rc)
                _write(os.path.join(self.path, "done.json"), {"rc": rc})
                return rc
            ready, _, _ = select.select([listener], [], [], POLL)
            if ready:
                self._answer(listener, group)

    @staticmethod
    def _answer(listener, group):
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(TIMEOUT)
            try:
                with connection.makefile("rb") as stream:
                    command = stream.readline(32)
                if command == b"kill\n":
                    group.kill()
                elif command != b"live\n":
                    raise ValueError("unknown process control command")
                response = {"live": True}
            except OSError as error:
                response = {"error": str(error), "errno": error.errno or errno.EIO}
            except ValueError as error:
                response = {"error": str(error), "errno": errno.EINVAL}
            with contextlib.suppress(OSError):
                connection.sendall(json.dumps(response).encode() + b"\n")


if __name__ == "__main__":
    result = ProcessControl(sys.argv[1]).serve()
    if result < 0:
        sig = -result
        if sig != signal.SIGKILL:
            signal.signal(sig, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {sig})
        os.kill(os.getpid(), sig)
    sys.exit(result)
