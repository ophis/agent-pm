"""The in_process Runtime: an InProcess's run iterated on a daemon thread into a queue; no child
process. A run that raises ends with its traceback as Stderr lines and code 1. stop() never
waits: what came before it is still polled, the code is -SIGKILL unless the run had ended, and the
run is closed at its next yield.
"""

import contextlib
import queue
import signal
import threading
import traceback
from collections.abc import Callable
from typing import Self

from agent_pm.session.adapters.base import Adapter, InProcess
from agent_pm.session.adapters.registry import Registry
from agent_pm.session.engine.types import (
    ClientBound,
    ConfigError,
    DriverError,
    Liveness,
    Mode,
    Prepared,
    SessionEvent,
    SessionInfo,
    Spec,
    Stderr,
)
from agent_pm.session.hosts.base import Host
from agent_pm.session.modes.base import Runtime
from agent_pm.utils import Display

END = object()


class InProcessRuntime(Runtime):
    kind = Mode.IN_PROCESS

    def __init__(self, prepared: Prepared):
        super().__init__(prepared)
        self.stopped, self.code = False, None
        self.items, self.lock, self.over = queue.Queue(), threading.Lock(), threading.Event()

    @classmethod
    def preflight(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        host: Host,
        display: Display = None,
    ) -> Prepared:
        return cls._prepare(spec, adapter, display=display)[0]

    @classmethod
    def _prepare(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        display: Display,
    ) -> tuple[Prepared, InProcess]:
        run = adapter.in_process()
        if run is None:
            raise cls._unsupported(spec)
        cls._no_tui_keys(spec, display)
        cls._selected(spec, adapter)
        spec = cls._noted(spec, adapter)
        if spec.sid is None:
            raise ConfigError("sid: missing")
        run.check(spec)
        cwd = spec.access.cwd or spec.workdir
        prepared = Prepared(spec, SessionInfo(spec.sid, cwd, spec.access.project, "", ""))
        return prepared, run

    @classmethod
    def start(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        record: Callable[[SessionInfo], None],
        host: Host,
        display: Display = None,
        check_name: Callable[[str], None] | None = None,
    ) -> Self:
        prepared, run = cls._prepare(spec, adapter, display=display)
        record(prepared.info)
        mode = cls(prepared)
        try:
            threading.Thread(target=mode._pump, args=(run, prepared.spec), daemon=True).start()
        except RuntimeError as e:
            raise DriverError(f"cannot start the in_process run: {e}") from e
        return mode

    def _pump(self, run: InProcess, spec: Spec) -> None:
        events, code = None, 1
        try:
            events = iter(run.run(spec))
            for item in events:
                with self.lock:
                    if self.stopped:
                        break
                    self.items.put(item)
            code = 0
        except BaseException as e:  # noqa: BLE001  # Forward failures to the caller thread.
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

    def poll(self, timeout: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
        with contextlib.suppress(queue.Empty):
            item = self.items.get(timeout=timeout)
            return ([], True) if item is END else ([item], False)
        return [], False

    def returncode(self, timeout: float) -> int | None:
        self.over.wait(timeout)
        return self.code

    def state(self) -> Liveness:
        return Liveness.ABSENT if self.over.is_set() else Liveness.LIVE

    def send(self, text: str) -> None:
        raise DriverError("send: tui mode only")

    def screen(self, lines: int | None = None) -> str:
        raise DriverError("screen: tui mode only")

    def stop(self) -> None:
        # A run blocked in a call goes on until its next yield or the process exits.
        with self.lock:
            if self.stopped:
                return
            self.stopped = True
            if not self.over.is_set():
                self.code = -signal.SIGKILL
            self.items.put(END)
            self.over.set()
