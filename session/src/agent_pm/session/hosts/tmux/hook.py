"""Host callback for tui activity, using the launch's trusted executable."""

import os
import stat
import subprocess

from agent_pm.session.engine.types import HookError
from agent_pm.session.hosts.tmux import tmux_host


def on_hook(word, _payload, state):
    executable = state.get("host_state", {}).get("executable")
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
    try:
        tmux_host.mark(word, state["events"], tmux=executable, proc=subprocess.run)
    except tmux_host.TuiError as error:
        raise HookError(str(error)) from None
