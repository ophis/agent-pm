"""A manager's directory, ~/.agent-pm/managers/<name>/: the fixed home of its `events` file, which workers.py (start,
next-event), drive.py (--runner tui, --detach) and router.py --tui use when given no --events (all but next-event
append to it).

<name> is [A-Za-z0-9_-]+, resolved (directory) as `--manager <name>`, else the caller's tmux session
(tui_claude.own_session), else none. The first command that resolves one creates it (ensure), and `events` (0600, a
regular file of the caller's: tui_claude.events_file). ~/.agent-pm is made when missing and not checked; managers/ and
<name>/ are made one level at a time, 0700 whatever the umask. One that exists keeps its mode but must be a directory
(not a symlink) owned by the caller, else ManagerError, so nothing is created through a bad managers/. Every failure is
a ManagerError.
"""
from __future__ import annotations

import os
import stat
import subprocess

import tui_claude

ROOT = "~/.agent-pm/managers"


class ManagerError(Exception):
    pass


def directory(manager: str | None = None, *, proc=subprocess.run) -> str | None:
    """The absolute path of the manager's directory, the filesystem untouched; None outside tmux without `manager`."""
    if manager is None:
        try:
            manager = tui_claude.own_session(proc=proc)
        except tui_claude.TuiError as e:
            raise ManagerError(f"{e}; give --manager <name>") from e
        if manager is None:
            return None
    elif not tui_claude.NAME.fullmatch(manager):
        raise ManagerError(f"manager {manager!r}: want {tui_claude.NAME.pattern}")
    return os.path.join(os.path.abspath(os.path.expanduser(ROOT)), manager)


def ensure(directory: str) -> None:
    """Creates directory (as `directory()` returns it) and checks it."""
    managers = os.path.dirname(directory)
    home = os.path.dirname(managers)
    try:
        os.makedirs(home, mode=0o700, exist_ok=True)
    except OSError as e:
        raise ManagerError(f"manager directory {home}: {e.strerror}") from e
    for path in (managers, directory):
        try:
            try:
                os.mkdir(path, 0o700)
                os.chmod(path, 0o700)
            except FileExistsError:
                pass
            st = os.lstat(path)
        except OSError as e:
            raise ManagerError(f"manager directory {path}: {e.strerror}") from e
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise ManagerError(f"manager directory {path}: not a directory owned by you")


def events(directory: str, *, create: bool = True) -> str:
    """The absolute path of directory's `events` file; with create, the directory and the file are made first."""
    directory = os.path.abspath(directory)
    path = os.path.join(directory, "events")
    if create:
        ensure(directory)
        try:
            return tui_claude.events_file(path)
        except tui_claude.TuiError as e:
            raise ManagerError(str(e)) from e
    return path
