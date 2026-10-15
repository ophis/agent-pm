"""Encode Delegate events, stamped with their run's sid and attempt, and deliver each line to the
configured callback command: synchronously, in its own process group, from the run dir, its stdout
discarded. A delivery that fails (exit ≠ 0, CALLBACK_TIMEOUT, OSError) is one `callback_error` row
in run.jsonl, never retried; an exception once the child exists kills and reaps the group, then
propagates. The child inherits the held, emptied callback lock, then the lock gets its pid.
"""

import errno
import fcntl
import os
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import asdict

from agent_pm.delegate.engine.state import RunState
from agent_pm.delegate.engine.types import Begun, Event, Progress
from agent_pm.delegate.event import DelegateEvent
from agent_pm.session import RunLog
from agent_pm.utils import kill_group

CALLBACK_TIMEOUT = 300  # seconds: longer than the worst-case write-back


class EventCallback:
    def __init__(self, command: Sequence[str], store: RunState, *, sid: str, attempt: int):
        self.command = tuple(command)
        self.store = store
        self.sid, self.attempt = sid, attempt

    def encode(self, event: Event) -> DelegateEvent:
        run = {"sid": self.sid, "attempt": self.attempt}
        if isinstance(event, Begun):
            return DelegateEvent("begun", **run)
        if isinstance(event, Progress):
            return DelegateEvent("progress", **run, name=event.name, text=event.text)
        result = event.result
        return DelegateEvent(
            "finished",
            **run,
            returncode=result.returncode,
            outcome=None if result.outcome is None else asdict(result.outcome),
            error=result.error,
            limit=None if result.limit is None else asdict(result.limit),
        )

    def __call__(self, event: Event) -> None:
        line = self.encode(event).to_line() + "\n"
        lock = self.store.callback_lock
        try:
            descriptor = _hold(lock)
        except BlockingIOError:
            self._failed(f"callback lock {lock} held: an earlier callback still runs")
            return
        except OSError as error:
            self._failed(str(error))
            return
        try:
            self._deliver(line, descriptor)
        finally:
            os.close(descriptor)

    def _deliver(self, line: str, lock: int) -> None:
        child = None
        try:
            try:
                child = subprocess.Popen(
                    self.command,
                    cwd=self.store.layout.run,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    text=True,
                    process_group=0,
                    pass_fds=(lock,),
                )
            except OSError as error:
                self._failed(str(error))
                return
            with child:
                try:
                    try:
                        os.write(lock, f"{child.pid}\n".encode())
                    except OSError as error:
                        kill_group(child)
                        self._failed(f"callback lock {self.store.callback_lock}: {error}")
                        return
                    child.communicate(line, timeout=CALLBACK_TIMEOUT)
                except subprocess.TimeoutExpired:
                    kill_group(child)
                    self._failed(f"callback timed out after {CALLBACK_TIMEOUT} s")
                    return
                except BaseException:
                    kill_group(child)
                    raise
            if child.returncode:
                self._failed(f"callback exited {child.returncode}")
        except BaseException:
            if child is not None and child.returncode is None:
                kill_group(child)
            raise

    def _failed(self, error: str) -> None:
        RunLog(self.store.layout.run).note("callback_error", error=error, best_effort=True)


def _hold(lock: str) -> int:
    """Held and emptied; BlockingIOError while held elsewhere."""
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "not a regular lock file", lock)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(descriptor, 0)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor
