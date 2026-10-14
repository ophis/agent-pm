"""The headless Mode: a Headless's command on pipes, leading its own process group; two daemon
threads pump its events and Stderr lines into one queue, and the session ends at both pipes' EOF.
stop() kills the whole group, so a grandchild holding the pipes dies too; every wait is bounded.
"""

import queue
import subprocess
import threading
from collections.abc import Callable, Iterable
from typing import Self

from agent_pm.session.adapters.base import Adapter, Headless
from agent_pm.session.adapters.registry import Registry
from agent_pm.session.engine.types import (
    ClientBound,
    DriverError,
    Liveness,
    Mode,
    Prepared,
    SessionEvent,
    SessionInfo,
    Spec,
    Stderr,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes.base import RunningMode, identity_drop
from agent_pm.utils import STOP_WAIT, Display, kill_group


class HeadlessMode(RunningMode):
    kind = Mode.HEADLESS
    stop_wait = STOP_WAIT

    def __init__(self, prepared: Prepared, proc: subprocess.Popen):
        super().__init__(prepared)
        self.proc, self.items, self.open = proc, queue.Queue(), 2
        self.eof = (threading.Event(), threading.Event())

    @classmethod
    def preflight(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        host=tmux_host,
        display: Display = None,
    ) -> Prepared:
        return cls._prepare(spec, adapter, registry=registry, display=display)[0]

    @classmethod
    def _prepare(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        display: Display,
    ) -> tuple[Prepared, Headless]:
        run = adapter.headless()
        if run is None:
            raise cls._unsupported(spec)
        cls._no_tui_keys(spec, display)
        cls._selected(spec, adapter)
        drop = identity_drop(registry)
        spec = cls._noted(spec, adapter)
        return cls._launched(spec, run.launch, drop), run

    @classmethod
    def start(
        cls,
        spec: Spec,
        adapter: Adapter,
        *,
        registry: Registry,
        record: Callable[[SessionInfo], None],
        host=tmux_host,
        display: Display = None,
    ) -> Self:
        prepared, run = cls._prepare(spec, adapter, registry=registry, display=display)
        run.commit()
        record(prepared.info)
        try:
            proc = subprocess.Popen(
                prepared.argv,
                cwd=prepared.info.cwd,
                env=dict(prepared.env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                process_group=0,
            )
            mode = cls(prepared, proc)
            out, err = proc.stdout, proc.stderr
            assert out is not None and err is not None
            for pipe, events, eof in (
                (out, run.events(out), mode.eof[0]),
                (err, (Stderr(line.rstrip("\n")) for line in err), mode.eof[1]),
            ):
                threading.Thread(target=mode._pump, args=(pipe, events, eof), daemon=True).start()
        except (OSError, ValueError) as e:
            raise DriverError(f"cannot start {prepared.argv[0]}: {e}") from e
        return mode

    def _pump(
        self, pipe, events: Iterable[SessionEvent | Stderr | ClientBound], eof: threading.Event
    ) -> None:
        try:
            with pipe:
                for event in events:
                    self.items.put(event)
        except BaseException as e:  # noqa: BLE001  # Forward failures to the caller thread.
            self.items.put(e)
        else:
            eof.set()
            self.items.put(None)

    def poll(self, timeout: float) -> tuple[list[SessionEvent | Stderr | ClientBound], bool]:
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

    def state(self) -> Liveness:
        return Liveness.LIVE if self.proc.poll() is None else Liveness.ABSENT

    def send(self, text: str) -> None:
        raise DriverError("send: tui mode only")

    def screen(self, lines: int | None = None) -> str:
        raise DriverError("screen: tui mode only")

    def stop(self) -> None:
        kill_group(self.proc, ended=all(eof.is_set() for eof in self.eof), wait=self.stop_wait)
