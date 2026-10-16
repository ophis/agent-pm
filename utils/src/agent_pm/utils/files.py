import contextlib
import errno
import fcntl
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path


def read_regular(
    path: str | os.PathLike,
    *,
    offset: int = 0,
    limit: int | None = None,
    tail: bool = False,
) -> bytes | None:
    """From byte `offset`: to the end, else the first `limit` bytes (the last with `tail`); None: no
    regular file, an OSError. Never waits on a FIFO.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
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


def holds(path: str | Path, text: str) -> bool:
    """Whether `path` is a regular file holding exactly `text`."""
    data = text.encode()
    return read_regular(path, limit=len(data) + 1) == data


@contextlib.contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """Exclusive stable <path>.lock, non-inheritable. Event writers acquire it before opening
    the event file and hold it through descriptor close, without acquiring a roster lock.
    Rotators hold it through physical changes and durable ledger save; never replace this lock.
    """
    lock = str(path) + ".lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(errno.EINVAL, "not a regular lock file", lock)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def inode_lock(path: str | Path, *, wait: bool = True) -> Iterator[bool]:
    """Exclusive flock on `path` itself, held once `path` still names the locked file, so a holder
    may unlink it; False without waiting when another holds it.
    """
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "not a regular lock file", str(path))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            try:
                same = os.path.samestat(os.fstat(fd), os.stat(path))
            except FileNotFoundError:
                same = False
            if same:
                yield True
                return
        finally:
            os.close(fd)


def sync_directory(path: str | Path) -> None:
    """Persist directory entries after rename or unlink; filesystem errors propagate."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_line(
    path: str | Path,
    text: str | bytes,
    *,
    durable: bool = False,
    locked: bool = False,
) -> None:
    """Refuses anything but a regular file, never waits on a FIFO; `durable` syncs it and its
    directory; `locked` uses file_lock.
    """
    data = memoryview(text.encode(errors="replace") if isinstance(text, str) else text)
    with file_lock(path) if locked else contextlib.nullcontext():
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, "not a regular file", str(path))
            while data:
                data = data[os.write(fd, data) :]
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)
        if durable:
            sync_directory(os.path.dirname(os.path.abspath(path)))


def save(path: str | Path, text: str | bytes, *, durable: bool = False) -> None:
    """Writes atomically, replacing a symlink without following. `durable` syncs the replacement
    and its directory.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".tmp-", delete=False) as f:
        try:
            f.write(text.encode() if isinstance(text, str) else text)
            if durable:
                f.flush()
                os.fsync(f.fileno())
            f.close()
            os.replace(f.name, path)
        except BaseException:
            os.unlink(f.name)
            raise
    if durable:
        sync_directory(path.parent)


class ConfinedReason(StrEnum):
    REGULAR = "regular"
    SUFFIX = "suffix"
    OUTSIDE = "outside"
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
    real = _descriptor_path(fd)
    if real.suffix != suffix:
        raise ConfinedFileError(ConfinedReason.SUFFIX, path)
    if not real.is_relative_to(root):
        raise ConfinedFileError(ConfinedReason.OUTSIDE, path)
    return opened


def _within_limit(path: str, size: int, limit: int) -> None:
    if size > limit:
        raise ConfinedFileError(ConfinedReason.TOO_LARGE, path, limit=limit)


def read_confined(path: str, root: str, *, limit: int, suffix: str = ".md") -> bytes:
    """
    Reads at most `limit` bytes of a regular `suffix` file under `root` (by its open descriptor's
    path). OSError reports filesystem failures; ConfinedFileError reports confinement failures.
    """
    base = Path(root).resolve()
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        opened = _checked(fd, path, base, suffix)
        _within_limit(path, opened.st_size, limit)
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(limit + 1)
        _within_limit(path, len(data), limit)
        return data
    finally:
        os.close(fd)
