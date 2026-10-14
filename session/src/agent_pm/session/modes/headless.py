"""The headless Mode: a Headless's command on pipes, leading its own process group; two daemon
threads pump its events and Stderr lines into one queue, and the session ends at both pipes' EOF.
stop() kills the whole group, so a grandchild holding the pipes dies too; every wait is bounded.
probe() runs a short command for its stdout.
"""

import contextlib
import os
import queue
import signal
import subprocess
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Self

from agent_pm.session.adapters.base import Adapter
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
from agent_pm.utils import Display

STOP_WAIT = 5


def kill_group(proc: subprocess.Popen, *, ended: bool = False, wait: float = STOP_WAIT) -> None:
    # ended (both pipes closed) and proc reaped: the group is gone and its id may be reused, so
    # signal nothing.
    if not (ended and proc.returncode is not None):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=wait)


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
        assert proc.stdout is not None
        proc.stdout.close()


class HeadlessMode(RunningMode):
    kind = Mode.HEADLESS
    stop_wait = STOP_WAIT

    def __init__(self, prepared: Prepared, proc: subprocess.Popen):
        super().__init__(prepared)
        self.proc, self.items, self.open = proc, queue.Queue(), 2
        self.eof = (threading.Event(), threading.Event())

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
        run = adapter.headless()
        if run is None:
            raise cls._unsupported(spec)
        cls._no_tui_keys(spec, display)
        cls._selected(spec, adapter)
        drop = identity_drop(registry)
        spec = cls._noted(cls._no_tui_fields(spec), adapter)
        prepared = cls._launched(spec, run.launch, drop)
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
