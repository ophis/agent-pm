import errno
import fcntl
import os
import stat
import sys
import tempfile
from enum import Enum
from pathlib import Path


def read_regular(
    path: str | os.PathLike,
    *,
    offset: int = 0,
    limit: int | None = None,
    tail: bool = False,
    own: bool = False,
) -> bytes | None:
    """From byte `offset`: to the end, else the first `limit` bytes (the last with `tail`); None: a
    symlink, no regular file, an OSError. Never waits on a FIFO; own also checks ownership.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or own and opened.st_uid != os.getuid():
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            f.seek(offset)
            if limit is None:
                return f.read()
            if tail:
                f.seek(max(f.seek(0, os.SEEK_END) - limit, offset))
            return f.read(limit)
    except OSError:
        return None
    finally:
        os.close(fd)


def append_line(
    path: str | Path, text: str | bytes, *, durable: bool = False, own: bool = False
) -> None:
    """Refuses a symlink or anything but a regular file, never waits on a FIFO; `durable` syncs it
    and its directory; `own` also refuses one another user owns (EPERM).
    """
    data = memoryview(text.encode(errors="replace") if isinstance(text, str) else text)
    fd = os.open(
        path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        if own and st.st_uid != os.getuid():
            raise OSError(errno.EPERM, "not owned by you", str(path))
        while data:
            data = data[os.write(fd, data) :]
        if durable:
            os.fsync(fd)
    finally:
        os.close(fd)
    if durable:
        parent = os.open(
            os.path.dirname(os.path.abspath(path)), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def save(path: str | Path, text: str, *, own: bool = False, durable: bool = False) -> None:
    """Writes atomically. `own` refuses a nonregular or foreign destination; otherwise replaces
    symlinks without following. `durable` syncs the replacement and its directory.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if own:
        try:
            opened = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(errno.EINVAL, "not a regular file", str(path))
            if opened.st_uid != os.getuid():
                raise OSError(errno.EPERM, "not owned by you", str(path))
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".tmp-", delete=False) as f:
        try:
            f.write(text)
            if durable:
                f.flush()
                os.fsync(f.fileno())
            f.close()
            os.replace(f.name, path)
        except BaseException:
            os.unlink(f.name)
            raise
    if durable:
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


class ConfinedReason(Enum):
    REGULAR = "regular"
    SINGLE_LINK = "single_link"
    SUFFIX = "suffix"
    OUTSIDE = "outside"
    REPLACED = "replaced"
    TOO_LARGE = "too_large"


class ConfinedFileError(ValueError):
    def __init__(self, reason: ConfinedReason, path: str, *, limit: int | None = None):
        self.reason = reason
        self.path = path
        self.limit = limit
        super().__init__(
            f"{path!r}: {reason.value}" + (f" (limit {limit})" if limit is not None else "")
        )


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
