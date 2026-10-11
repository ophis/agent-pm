"""The Driver's headless Mode: a HeadlessRun's command on pipes, stdin /dev/null. Two daemon threads pump the run's
events() over stdout and one Stderr per stderr line into one queue, each closing its pipe when done; the session
has ended at both pipes' EOF. Imports only the stdlib and adapters."""
import queue
import subprocess
import threading
from collections.abc import Iterable, Mapping, Sequence

from adapters.base import Event, HeadlessRun, Stderr


class HeadlessMode:
    """One headless session's process."""

    def __init__(self, run: HeadlessRun):
        self.run, self.proc = run, None

    def begin(self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str], popen=None) -> None:
        """Starts argv; popen's exception propagates and leaves nothing to stop."""
        popen = popen or subprocess.Popen   # looked up per call, so a patch of subprocess.Popen applies
        self.proc = popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, errors="replace")
        self.items, self.open = queue.Queue(), 2
        out, err = self.proc.stdout, self.proc.stderr
        for pipe, events in ((out, self.run.events(out)), (err, (Stderr(line.rstrip("\n")) for line in err))):
            threading.Thread(target=self._pump, args=(pipe, events), daemon=True).start()

    def _pump(self, pipe, events: Iterable[Event | Stderr]) -> None:
        try:
            with pipe:
                for event in events:
                    self.items.put(event)
        except BaseException as e:
            self.items.put(e)
        else:
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

    def returncode(self) -> int:
        """The process's exit status, waiting for it; negative: killed by that signal."""
        return self.proc.wait()

    def alive(self) -> bool:
        """Whether the process runs: False before begin and after exit."""
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        """Kills the process and waits; a no-op before begin, after a failed begin or after exit."""
        if self.proc is not None:
            self.proc.kill()
            self.proc.wait()
