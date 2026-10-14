"""Encode Delegate events, stamped with their run's sid and attempt, and deliver each line to the
configured callback command.
"""

import subprocess
from collections.abc import Sequence
from dataclasses import asdict

from agent_pm.delegate.engine.types import Begun, Event, Progress
from agent_pm.delegate.event import DelegateEvent
from agent_pm.session import RunLog


class EventCallback:
    def __init__(self, command: Sequence[str], workdir: str, *, sid: str, attempt: int):
        self.command = tuple(command)
        self.workdir = workdir
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
        try:
            completed = subprocess.run(
                self.command,
                input=self.encode(event).to_line() + "\n",
                cwd=self.workdir,
                text=True,
                stdout=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            message = str(error)
        else:
            if completed.returncode == 0:
                return
            message = f"callback exited {completed.returncode}"
        RunLog(self.workdir).note("callback_error", error=message, best_effort=True)
