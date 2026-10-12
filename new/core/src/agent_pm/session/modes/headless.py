"""The Driver's headless Mode: a Headless's command on pipes, leading its own process group; two daemon threads pump
its events and Stderr lines into one queue, and the session ends at both pipes' EOF. stop() kills the whole group, so a
grandchild holding the pipes dies too; every wait is bounded. probe() runs a short command for its stdout."""

import os
import queue
import signal
import subprocess
import threading
from collections.abc import Iterable, Mapping, Sequence

from agent_pm.session.adapters.base import Event, Headless, Stderr
from agent_pm.session.engine.types import DriverError

STOP_WAIT = 5


def kill_group(proc: subprocess.Popen, ended: bool = False) -> None:
    # ended (both pipes closed) and proc reaped: the group is gone and its id may be reused, so signal nothing.
    if not (ended and proc.returncode is not None):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    try:
        proc.wait(timeout=STOP_WAIT)
    except subprocess.TimeoutExpired:
        pass


def probe(argv: Sequence[str], *, cwd: str, env: Mapping[str, str], timeout: float) -> str | None:
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            process_group=0,
        )
    except (OSError, ValueError):
        return None
    try:
        return proc.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        kill_group(proc)
        return None
    finally:
        proc.stdout.close()


class HeadlessMode:
    def __init__(self, run: Headless):
        self.run, self.proc = run, None

    def begin(self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str], popen=None) -> None:
        popen = (
            popen or subprocess.Popen
        )  # looked up per call, so a patch of subprocess.Popen applies
        self.items, self.open = queue.Queue(), 2
        self.eof = (threading.Event(), threading.Event())
        self.proc = popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            process_group=0,
        )
        out, err = self.proc.stdout, self.proc.stderr
        for pipe, events, eof in (
            (out, self.run.events(out), self.eof[0]),
            (err, (Stderr(line.rstrip("\n")) for line in err), self.eof[1]),
        ):
            threading.Thread(target=self._pump, args=(pipe, events, eof), daemon=True).start()

    def _pump(self, pipe, events: Iterable[Event | Stderr], eof: threading.Event) -> None:
        try:
            with pipe:
                for event in events:
                    self.items.put(event)
        except BaseException as e:
            self.items.put(e)
        else:
            eof.set()
            self.items.put(None)

    def poll(self, timeout: float) -> tuple[list[Event | Stderr], bool]:
        try:
            item = self.items.get(timeout=timeout)
        except queue.Empty:
            return [], False
        if isinstance(item, BaseException):
            raise item
        if item is None:
            self.open -= 1
            return [], self.open == 0
        return [item], False

    def returncode(self, timeout: float) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def state(self) -> str:
        return "live" if self.proc is not None and self.proc.poll() is None else "absent"

    def send(self, text: str) -> None:
        raise DriverError("send: tui mode only")

    def screen(self, lines: int | None = None) -> str:
        raise DriverError("screen: tui mode only")

    def stop(self) -> None:
        if self.proc is not None:
            kill_group(self.proc, all(eof.is_set() for eof in self.eof))
