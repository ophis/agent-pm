"""A Docker session's host run supervisor (argv: name, state dir, TUI dir, events or "", hook
command, then relay, dead-wait and dead-check argv as JSON). Holding supervisor.lock, it runs the
relay; each valid request runs `<hook> hook <TUI dir> <label>`, and every line gets one ack. SIGTERM
closes the relay's stdin; lines already sent still run. A confirmed client exit writes the dead line.
"""

import base64
import contextlib
import fcntl
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time

from agent_pm.session.engine.status_log import SessionStatusLog
from agent_pm.session.hosts.docker.relay import LINE_MAX, request_id
from agent_pm.utils import timestamp

PAYLOAD_MAX = 64 << 20
HOOK_WAIT = 60
CHECK_WAIT = 10
START_WAIT = 60
POLL = 0.5
LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")
LOG_MAX = 1 << 20
LOG_TEXT = 200


class Supervisor:
    def __init__(
        self, name: str, state: str, tui_dir: str, events: str, hook: str, *, run=None
    ) -> None:
        self.name, self.state, self.tui_dir, self.events, self.hook = (
            name,
            state,
            tui_dir,
            events,
            hook,
        )
        self.run, self.popen = run or subprocess.run, subprocess.Popen
        self.stopping, self.line_max = False, LINE_MAX

    def log(self, why: str, line: bytes) -> None:
        path = os.path.join(self.state, "supervisor.log")
        with contextlib.suppress(OSError):
            if os.path.exists(path) and os.path.getsize(path) >= LOG_MAX:
                return
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{timestamp()} {why}: {repr(line[:LOG_TEXT])[:LOG_TEXT]}\n")

    def handle(self, line: bytes) -> dict:
        ran = self._hook(line)
        return {"id": request_id(line), "ok": ran}

    def _hook(self, line: bytes) -> bool:
        try:
            found = json.loads(line)
        except (ValueError, RecursionError):
            found = None
        if not (
            isinstance(found, dict)
            and found.keys() == {"id", "argv", "payload"}
            and request_id(line) is not None
        ):
            self.log("refused request", line)
            return False
        argv, payload = found["argv"], found["payload"]
        if not (
            isinstance(argv, list)
            and len(argv) == 3
            and argv[:2] == ["hook", self.tui_dir]
            and isinstance(argv[2], str)
            and LABEL.fullmatch(argv[2])
        ):
            self.log("refused argv", line)
            return False
        data = None
        with contextlib.suppress(ValueError):
            if isinstance(payload, str) and len(payload) <= (PAYLOAD_MAX + 2) // 3 * 4:
                data = base64.b64decode(payload, validate=True)
        try:
            body = json.loads(data) if data is not None and len(data) <= PAYLOAD_MAX else None
        except (ValueError, RecursionError):
            body = None
        if not isinstance(body, dict):
            self.log("refused payload", line)
            return False
        body.pop("transcript_path", None)
        try:
            self.run(
                [self.hook, "hook", self.tui_dir, argv[2]],
                input=json.dumps(body).encode(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=HOOK_WAIT,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self.log(f"hook failed ({type(error).__name__})", line)
            return False
        return True

    def serve(self, requests, acks) -> None:
        """An ack per line until EOF; a line over LINE_MAX is discarded."""
        while line := requests.readline(self.line_max + 1):
            if line.endswith(b"\n"):
                ack = self.handle(line)
            else:
                self.log("over-long line", line)
                while line and not line.endswith(b"\n"):
                    line = requests.readline(self.line_max + 1)
                ack = {"id": None, "ok": False}
            if not self.stopping:
                with contextlib.suppress(OSError, ValueError):
                    acks.write(json.dumps(ack).encode() + b"\n")
                    acks.flush()

    def watch(self, wait: list[str], check: list[str], stopped, start: float = START_WAIT) -> None:
        """A failed wait is retried for `start` s, until one has succeeded."""
        deadline = time.monotonic() + start
        while not stopped.is_set():
            try:
                waited = self.run(
                    wait,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                return
            if stopped.is_set():
                return
            if waited.returncode:
                if time.monotonic() >= deadline:
                    return
            elif self._dead(check):
                if self.events:
                    with contextlib.suppress(OSError, ValueError):
                        SessionStatusLog(self.events).write(self.name, "dead")
                return
            else:
                deadline = 0.0
            stopped.wait(POLL)

    def _dead(self, check: list[str]) -> bool:
        try:
            with self.popen(
                check, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            ) as proc:
                ready = select.select([proc.stdout], [], [], CHECK_WAIT)[0]
                out = os.read(proc.stdout.fileno(), 64) if ready else b""
                proc.kill()
        except OSError:
            return False
        return out.strip() == b"1"


def main(argv=None) -> int:
    try:
        name, state, tui_dir, events, hook, *commands = sys.argv[1:] if argv is None else argv
        relay, wait, check = (json.loads(command) for command in commands)
    except ValueError:
        return 2
    lock = os.open(os.path.join(state, "supervisor.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        return 1
    with open(os.path.join(state, "supervisor.pid"), "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    supervisor = Supervisor(name, state, tui_dir, events, hook)
    proc = subprocess.Popen(
        relay, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    stopped = threading.Event()

    def stop(_signum, _frame):
        stopped.set()
        supervisor.stopping = True
        if proc.stdin is not None:
            proc.stdin.close()

    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=supervisor.watch, args=(wait, check, stopped), daemon=True).start()
    supervisor.serve(proc.stdout, proc.stdin)
    stopped.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
