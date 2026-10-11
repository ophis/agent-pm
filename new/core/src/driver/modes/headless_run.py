"""The Driver's headless Mode: a HeadlessRun's command on pipes, stdin /dev/null, leading its own process group. Two
daemon threads pump the run's events() over stdout and one Stderr per stderr line into one queue, each closing its pipe
when done; the session has ended at both pipes' EOF. stop() kills the whole group, so a grandchild holding the pipes
dies with it; every wait on the process is bounded. probe() runs a short command for its stdout. Imports only the
stdlib and adapters."""
import os
import queue
import signal
import subprocess
import threading
from collections.abc import Iterable, Mapping, Sequence

from adapters.base import Event, HeadlessRun, Stderr

STOP_WAIT = 5   # seconds: the longest wait for a killed process to be reaped


def kill_group(proc: subprocess.Popen, ended: bool = False) -> None:
    """SIGKILL to the process group proc leads, else to proc alone, then reaps proc, waiting at most STOP_WAIT s.
    `ended`: both its pipes have ended; once proc is reaped too, the group is gone and its id may be reused, so nothing
    is signalled."""
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
    """argv's stdout, run in cwd with env, stdin /dev/null, stderr discarded, leading its own process group; None on a
    spawn error, or once `timeout` s pass (its group killed, it reaped)."""
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, errors="replace", process_group=0)
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
    """One headless session's process."""

    def __init__(self, run: HeadlessRun):
        self.run, self.proc = run, None

    def begin(self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str], popen=None) -> None:
        """Starts argv; popen's exception propagates and leaves nothing to stop."""
        popen = popen or subprocess.Popen   # looked up per call, so a patch of subprocess.Popen applies
        self.items, self.open = queue.Queue(), 2
        self.eof = (threading.Event(), threading.Event())   # set by each pipe's pump at its end
        self.proc = popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, errors="replace", process_group=0)
        out, err = self.proc.stdout, self.proc.stderr
        for pipe, events, eof in ((out, self.run.events(out), self.eof[0]),
                                  (err, (Stderr(line.rstrip("\n")) for line in err), self.eof[1])):
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
        """The next event, waiting up to `timeout` s, and whether both pipes have ended; a pump's exception is
        raised."""
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
        """The process's exit status, waiting at most `timeout` s; None while it runs; negative: killed by that
        signal."""
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def alive(self) -> bool:
        """Whether the process runs: False before begin and after exit."""
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        """kill_group on the process: its group killed, so whatever still holds the pipes ends too; a no-op before
        begin and after a failed begin."""
        if self.proc is not None:
            kill_group(self.proc, all(eof.is_set() for eof in self.eof))
