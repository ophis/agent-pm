"""Encode Delegate events, stamped with their run's sid and attempt, and deliver each line to the
configured callback command: synchronously, in its own process group, from the run dir, its stdout
discarded. A delivery that fails (exit ≠ 0, CALLBACK_TIMEOUT, OSError) is one `callback_error` row
in run.jsonl, never retried; an exception while waiting kills and reaps the group, then propagates.
"""

import subprocess
from collections.abc import Sequence
from dataclasses import asdict

from agent_pm.delegate.engine.types import Begun, Event, Progress
from agent_pm.delegate.event import DelegateEvent
from agent_pm.session import RunLog
from agent_pm.utils import kill_group

CALLBACK_TIMEOUT = 300  # seconds: longer than the worst-case write-back


class EventCallback:
    def __init__(self, command: Sequence[str], workdir: str, *, cwd: str, sid: str, attempt: int):
        self.command = tuple(command)
        self.workdir, self.cwd = workdir, cwd
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
        try:
            child = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                text=True,
                process_group=0,
            )
        except OSError as error:
            self._failed(str(error))
            return
        with child:
            try:
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

    def _failed(self, error: str) -> None:
        RunLog(self.workdir).note("callback_error", error=error, best_effort=True)
