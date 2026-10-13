"""The Driver's in_process Mode: an InProcess's run iterated on a daemon thread into a queue; no child process. A run
that raises ends with its traceback as Stderr lines and code 1. stop() never waits: what came before it is still
polled, the code is -SIGKILL unless the run had ended, and the run is closed at its next yield."""

import contextlib
import queue
import signal
import threading
import traceback

from agent_pm.session.adapters.base import Event, InProcess, Spec, Stderr
from agent_pm.session.engine.types import DriverError

END = object()


class InProcessMode:
    def __init__(self, run: InProcess):
        self.run, self.begun, self.stopped, self.code = run, False, False, None
        self.items, self.lock, self.over = queue.Queue(), threading.Lock(), threading.Event()

    def begin(self, prompt: str, spec: Spec, resume: bool) -> None:
        try:
            threading.Thread(target=self._pump, args=(prompt, spec, resume), daemon=True).start()
        except RuntimeError as e:
            raise DriverError(f"cannot start the in_process run: {e}") from e
        self.begun = True

    def _pump(self, prompt: str, spec: Spec, resume: bool) -> None:
        events, code = None, 1
        try:
            events = iter(self.run.run(prompt, spec, resume))
            for item in events:
                with self.lock:
                    if self.stopped:
                        break
                    self.items.put(item)
            code = 0
        except BaseException as e:
            lines = "".join(traceback.format_exception(e)).rstrip().split("\n")
            with self.lock:
                if not self.stopped:
                    for line in lines:
                        self.items.put(Stderr(line.rstrip()))
        finally:
            if self.stopped:
                with contextlib.suppress(BaseException):
                    getattr(events, "close", lambda: None)()
            with self.lock:
                if not self.stopped:
                    self.code = code
                    self.items.put(END)
                self.over.set()

    def poll(self, timeout: float) -> tuple[list[Event | Stderr], bool]:
        try:
            item = self.items.get(timeout=timeout)
        except queue.Empty:
            return [], False
        return ([], True) if item is END else ([item], False)

    def returncode(self, timeout: float) -> int | None:
        self.over.wait(timeout)
        return self.code

    def state(self) -> str:
        return "live" if self.begun and not self.over.is_set() else "absent"

    def send(self, text: str) -> None:
        raise DriverError("send: tui mode only")

    def screen(self, lines: int | None = None) -> str:
        raise DriverError("screen: tui mode only")

    def stop(self) -> None:
        # A run blocked in a call goes on until its next yield or the process exits.
        with self.lock:
            if not self.begun or self.stopped:
                return
            self.stopped = True
            if not self.over.is_set():
                self.code = -signal.SIGKILL
            self.items.put(END)
            self.over.set()
