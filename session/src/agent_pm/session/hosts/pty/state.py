"""A PtyHost session's dir, ~/.agent-pm/pty/<name>/: meta.json (PtyHost's), supervisor.json (the
supervisor's: its pid, then also the program's rc), activity (the tui hook's), output (the pty's), control (the supervisor's input
socket, a link into the session's ProcessControl dir).
"""

import contextlib
import json
import os

from agent_pm.session.engine.status_log import SessionStatusLog
from agent_pm.session.engine.types import StatusState
from agent_pm.utils import STATE

META = "meta.json"
SUPERVISOR = "supervisor.json"
ACTIVITY = "activity"
OUTPUT = "output"
CONTROL = "control"
SOCKET = "input"  # in the session's ProcessControl dir


def root() -> str:
    return os.path.join(os.path.expanduser(STATE), "pty")


def read(folder: str, file: str) -> str | None:
    try:
        with open(os.path.join(folder, file), encoding="utf-8") as stream:
            return stream.read()
    except OSError:
        return None


def meta(folder: str) -> dict:
    return _object(folder, META)


def supervisor(folder: str) -> dict:
    return _object(folder, SUPERVISOR)


def _object(folder: str, file: str) -> dict:
    try:
        found = json.loads(read(folder, file) or "{}")
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def put(folder: str, file: str, text: str, *, dir_fd: int | None = None) -> None:
    """Atomically; never creates `folder`, so a write after its removal fails."""
    temporary = f".{file}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    path = temporary if dir_fd is not None else os.path.join(folder, temporary)
    fd = os.open(path, flags, 0o600, dir_fd=dir_fd)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
    if dir_fd is not None:
        os.replace(temporary, file, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    else:
        os.replace(os.path.join(folder, temporary), os.path.join(folder, file))


def mark(folder: str, word: StatusState, events: str | None) -> None:
    """activity := word; done and blocked also go to events."""
    with contextlib.suppress(FileNotFoundError):
        put(folder, ACTIVITY, word)
    if events and word in (StatusState.DONE, StatusState.BLOCKED):
        SessionStatusLog(events).write(os.path.basename(folder), word)
