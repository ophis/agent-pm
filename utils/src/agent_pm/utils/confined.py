"""Bounded reads of single-link files confined to a directory, checked through their descriptor."""

import fcntl
import os
import stat
import sys
from pathlib import Path

from agent_pm.utils.types import ConfinedFileError, ConfinedReason


def _descriptor_path(fd: int) -> Path:
    if sys.platform == "darwin":
        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0]
        return Path(os.fsdecode(raw)).resolve()
    return Path(os.readlink(f"/proc/self/fd/{fd}")).resolve()


def _checked(fd: int, path: str, root: Path, suffix: str) -> os.stat_result:
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise ConfinedFileError(ConfinedReason.REGULAR, path)
    if opened.st_nlink != 1:
        raise ConfinedFileError(ConfinedReason.SINGLE_LINK, path)
    real = _descriptor_path(fd)
    if real.suffix != suffix:
        raise ConfinedFileError(ConfinedReason.SUFFIX, path)
    if not real.is_relative_to(root):
        raise ConfinedFileError(ConfinedReason.OUTSIDE, path)
    identity = opened.st_dev, opened.st_ino
    for current in (os.stat(path), real.stat()):
        if (current.st_dev, current.st_ino) != identity:
            raise ConfinedFileError(ConfinedReason.REPLACED, path)
    return opened


def _within_limit(path: str, size: int, limit: int) -> None:
    if size > limit:
        raise ConfinedFileError(ConfinedReason.TOO_LARGE, path, limit=limit)


def read_confined(path: str, root: str, *, limit: int, suffix: str = ".md") -> bytes:
    """
    Checks descriptor location and path identity before and after a bounded read. OSError reports
    filesystem failures; ConfinedFileError reports ownership-independent confinement failures.
    """
    base = Path(root).resolve()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = _checked(fd, path, base, suffix)
        _within_limit(path, opened.st_size, limit)
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(limit + 1)
        current = _checked(fd, path, base, suffix)
        _within_limit(path, max(len(data), current.st_size), limit)
        return data
    finally:
        os.close(fd)
