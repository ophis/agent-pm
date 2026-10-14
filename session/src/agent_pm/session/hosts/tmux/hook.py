"""Host callback for tui activity, using the launch's trusted executable."""

import os
import stat
import subprocess

from agent_pm.session.engine.types import HookError
from agent_pm.session.hosts.tmux import tmux_host, transport


def on_hook(word, _payload, state):
    saved = state.get("host_state", {})
    executable = saved.get("executable")
    if not isinstance(executable, str) or not os.path.isabs(executable):
        raise HookError("host_state: bad executable")
    try:
        info = os.stat(executable)
    except OSError as error:
        raise HookError(f"tmux {executable}: {error.strerror}") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in (0, os.getuid())
        or not os.access(executable, os.X_OK)
    ):
        raise HookError(f"tmux {executable}: not a regular executable file owned by you or root")
    target, generation = saved.get("target"), saved.get("generation")
    if not isinstance(target, str) or not transport.NAME.fullmatch(target):
        raise HookError("host_state: bad target")
    if not isinstance(generation, str) or not generation:
        raise HookError("host_state: bad generation")
    selectors = saved.get("environment")
    if not isinstance(selectors, dict) or any(
        key not in ("TMUX", "TMUX_TMPDIR") or not isinstance(value, str) or "\0" in value
        for key, value in selectors.items()
    ):
        raise HookError("host_state: bad environment")
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
                "#{pane_id}\t#{@sid}\t#{@generation}",
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=tmux_host.MARK_TIMEOUT,
        )
        if result.returncode:
            if transport.absent(result):
                return
            raise HookError(f"tmux: {transport.error(result)}")
        fields = result.stdout.strip().split("\t")
        if len(fields) != 3:
            return
        pane, sid, actual_generation = fields
        if not transport.PANE.fullmatch(pane):
            return
        if sid != state["sid"] or actual_generation != generation:
            return
        tmux_host.mark(word, state["events"], pane=pane, tmux=executable, proc=run)
    except subprocess.TimeoutExpired:
        raise HookError(f"tmux: timed out after {tmux_host.MARK_TIMEOUT} s") from None
    except OSError as error:
        raise HookError(f"tmux: {error}") from error
    except tmux_host.TuiError as error:
        raise HookError(str(error)) from None
