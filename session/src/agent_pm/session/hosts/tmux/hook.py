"""The tmux activity marker (mark) and the Host callback that runs it for tui hooks, using the
launch's trusted executable.
"""

import os
import stat
import subprocess

from agent_pm.session.engine.status_log import SessionStatusLog
from agent_pm.session.engine.types import HookCall, HookError
from agent_pm.session.hosts.tmux import transport
from agent_pm.session.hosts.tmux.types import TuiError

MARK_TIMEOUT = 5


def on_hook(word: str, call: HookCall) -> None:
    saved = call.state
    executable = saved.get("executable")
    if not isinstance(executable, str) or not os.path.isabs(executable):
        raise HookError("host state: bad executable")
    try:
        info = os.stat(executable)
    except OSError as error:
        raise HookError(f"tmux {executable}: {error.strerror}") from None
    if not stat.S_ISREG(info.st_mode) or not os.access(executable, os.X_OK):
        raise HookError(f"tmux {executable}: not a regular executable file")
    target, generation = saved.get("target"), saved.get("generation")
    if not isinstance(target, str) or not transport.NAME.fullmatch(target):
        raise HookError("host state: bad target")
    if not isinstance(generation, str) or not generation:
        raise HookError("host state: bad generation")
    selectors = saved.get("environment")
    if not isinstance(selectors, dict) or any(
        key not in ("TMUX", "TMUX_TMPDIR") or not isinstance(value, str) or "\0" in value
        for key, value in selectors.items()
    ):
        raise HookError("host state: bad environment")
    environment = {
        key: value for key, value in os.environ.items() if key not in ("TMUX", "TMUX_TMPDIR")
    }
    environment.update(selectors)

    def run(argv, **options):
        return subprocess.run(argv, env=environment, **options)

    try:
        result = run(
            [
                executable,
                "display-message",
                "-p",
                "-t",
                f"={target}:",
                "#{@process}\t#{@sid}\t#{@generation}",
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=MARK_TIMEOUT,
        )
        if result.returncode:
            if transport.absent(result):
                return
            raise HookError(f"tmux: {transport.error(result)}")
        fields = result.stdout.strip().split("\t")
        if len(fields) != 3:
            return
        process, sid, actual_generation = fields
        if not transport.PROCESS_ID.fullmatch(process):
            return
        if sid != call.sid or actual_generation != generation:
            return
        Hook(run).mark(word, call.events, pane=process.split(" ")[2], tmux=executable)
    except subprocess.TimeoutExpired:
        raise HookError(f"tmux: timed out after {MARK_TIMEOUT} s") from None
    except OSError as error:
        raise HookError(f"tmux: {error}") from error
    except TuiError as error:
        raise HookError(str(error)) from None


class Hook(transport.Transport):
    def mark(
        self,
        word: str,
        events: str | None,
        *,
        pane: str | None = None,
        tmux: str = "tmux",
    ) -> None:
        """Hook protocol: sets the pane ($TMUX_PANE when None) @state to word; done and blocked also
        append `HH:MM:SS <session> <word>` to events, an existing regular file (None: no line).
        """
        pane = os.environ.get("TMUX_PANE", "") if pane is None else pane
        if not pane:
            return
        if not transport.PANE.fullmatch(pane):
            raise TuiError(f"bad tmux pane {pane!r}")
        self._mark_tmux([tmux, "set-option", "-t", pane, "@state", word])
        if word not in ("done", "blocked") or events is None:
            return
        session = self._mark_tmux([tmux, "display-message", "-p", "-t", pane, "#S"]).strip()
        if not transport.NAME.fullmatch(session):
            raise TuiError(f"tmux session {session!r}: want [A-Za-z0-9_-]+")
        try:
            # O_NONBLOCK: a FIFO without a reader fails instead of hanging the hook
            fd = os.open(events, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise TuiError(f"events file {events}: not a regular file")
                os.write(fd, (SessionStatusLog.line(session, word) + "\n").encode())
            finally:
                os.close(fd)
        except OSError as e:
            raise TuiError(f"events file {events}: {e.strerror}") from e

    def _mark_tmux(self, argv: list[str]) -> str:
        try:
            res = self.proc(
                argv,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=MARK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise TuiError(f"tmux: timed out after {MARK_TIMEOUT} s") from None
        except OSError as e:
            raise TuiError(f"tmux: {e}") from e
        if res.returncode:
            raise TuiError(f"tmux: {transport.error(res)}")
        return res.stdout
