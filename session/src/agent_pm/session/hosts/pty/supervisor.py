"""PtyHost's supervisor, run as `python -P -s -m` this module with a session dir and an events
path ("" none), as a session leader: it holds the pty master and runs the session's ProcessControl
runner on the slave, its controlling terminal. It keeps the program's output in `output`, writes
its input socket's requests to the master, and once the program exits writes `exited <rc>`
(detach) or `dead` (terminal) and rc; a detach's dir then goes.
"""

import contextlib
import fcntl
import json
import os
import selectors
import shutil
import socket
import struct
import subprocess
import sys
import termios

from agent_pm.session.engine.status_log import SessionStatusLog
from agent_pm.session.hosts.pty import state
from agent_pm.utils import ProcessControl

ROWS, COLUMNS = 50, 160
CHUNK = 1 << 16
OUTPUT_MAX = 1 << 20  # bytes of output kept, then it starts over
REQUEST_MAX = 64 << 20
TIMEOUT = 10
POLL = 0.05
PASTE_ON, PASTE_OFF = b"\x1b[?2004h", b"\x1b[?2004l"
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"


def bracketing(seen: bytes, *, was: bool) -> bool:
    """Whether the program wants bracketed paste once it wrote `seen`."""
    on, off = seen.rfind(PASTE_ON), seen.rfind(PASTE_OFF)
    return was if on == off else on > off


def request(line: bytes, *, bracketed: bool) -> bytes:
    """An input request's bytes for the master: {"paste": text} or {"keys": hex}; ValueError."""
    found = json.loads(line)
    if isinstance(found, dict) and set(found) == {"paste"} and isinstance(found["paste"], str):
        data = found["paste"].encode()
        return PASTE_START + data + PASTE_END if bracketed else data
    if isinstance(found, dict) and set(found) == {"keys"} and isinstance(found["keys"], str):
        return bytes.fromhex(found["keys"])
    raise ValueError("want {paste: text} or {keys: hex}")


class Supervisor:
    def __init__(self, master: int, listener: socket.socket, output: int) -> None:
        self.master, self.listener, self.output = master, listener, output
        self.pending = bytearray()
        self.bracketed = False
        self.tail = b""

    def serve(self, runner: subprocess.Popen) -> int:
        selector = selectors.DefaultSelector()
        selector.register(self.master, selectors.EVENT_READ)
        selector.register(self.listener, selectors.EVENT_READ)
        while runner.poll() is None:
            wanted = selectors.EVENT_READ | (selectors.EVENT_WRITE if self.pending else 0)
            selector.modify(self.master, wanted)
            for key, mask in selector.select(POLL):
                if key.fileobj is self.listener:
                    self.answer()
                    continue
                if mask & selectors.EVENT_READ:
                    self.drain()
                if mask & selectors.EVENT_WRITE:
                    with contextlib.suppress(BlockingIOError):
                        del self.pending[: os.write(self.master, self.pending[:CHUNK])]
        return runner.returncode

    def drain(self) -> None:
        try:
            data = os.read(self.master, CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        seen = self.tail + data
        self.bracketed = bracketing(seen, was=self.bracketed)
        self.tail = seen[1 - len(PASTE_ON) :]
        if os.fstat(self.output).st_size > OUTPUT_MAX:
            os.ftruncate(self.output, 0)
        os.write(self.output, data)

    def answer(self) -> None:
        connection, _ = self.listener.accept()
        with connection:
            connection.settimeout(TIMEOUT)
            try:
                with connection.makefile("rb") as stream:
                    line = stream.readline(REQUEST_MAX)
                self.pending += request(line, bracketed=self.bracketed)
                reply = {"ok": True}
            except (OSError, ValueError, RecursionError) as error:
                reply = {"error": str(error)}
            with contextlib.suppress(OSError):
                connection.sendall(json.dumps(reply).encode() + b"\n")


def main(folder: str, events: str) -> None:
    meta = state.meta(folder)
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    os.set_blocking(master, False)
    control = ProcessControl(meta["control"])
    output = os.open(state.OUTPUT, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600, dir_fd=directory)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(os.path.join(control.path, state.SOCKET))
        listener.listen()
        runner = subprocess.Popen(
            control.command(), stdin=slave, stdout=slave, stderr=slave, cwd="/"
        )
        state.put(folder, state.PID, str(os.getpid()), dir_fd=directory)
        rc = Supervisor(master, listener, output).serve(runner)
    detach = meta["kind"] == "detach"
    if events:
        with contextlib.suppress(OSError):
            log, name = SessionStatusLog(events), os.path.basename(folder)
            if detach:
                log.write(name, "exited", rc)
            else:
                log.write(name, "dead")
    with contextlib.suppress(OSError):
        state.put(folder, state.RC, str(rc), dir_fd=directory)
    if detach and _same(folder, directory):
        shutil.rmtree(folder, ignore_errors=True)


def _same(folder: str, directory: int) -> bool:
    """`folder` is still the dir this supervisor opened, not a later session's."""
    try:
        return os.stat(folder).st_ino == os.fstat(directory).st_ino
    except OSError:
        return False


if __name__ == "__main__":
    main(*sys.argv[1:])
