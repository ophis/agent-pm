import errno
import os
import stat
import tempfile
from pathlib import Path


def read_regular(path: str | os.PathLike, *, offset: int = 0, limit: int | None = None,
                 tail: bool = False) -> bytes | None:
    """From byte `offset`: to the end, else the first `limit` bytes (the last with `tail`); None: a symlink, no regular
    file, an OSError. Never waits on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):   # first: fdopen raises on a directory, leaving fd open
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


def append_line(path: str | Path, text: str, *, durable: bool = False) -> None:
    """Refuses a symlink or anything but a regular file, never waits on a FIFO; `durable` syncs it and its directory."""
    data = memoryview(text.encode(errors="replace"))
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        while data:
            data = data[os.write(fd, data):]
        if durable:
            os.fsync(fd)
    finally:
        os.close(fd)
    if durable:
        parent = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def save(path: str | Path, text: str) -> None:
    """Writes `text` to `path` atomically; replaces a symlink planted there, never follows it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".tmp-", delete=False) as f:
        try:
            f.write(text)
            f.close()
            os.replace(f.name, path)
        except BaseException:
            os.unlink(f.name)
            raise
