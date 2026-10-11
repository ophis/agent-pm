"""Reads of files another process may control: read_regular never follows a symlink and never waits on a FIFO."""
import os
import stat


def read_regular(path: str | os.PathLike, *, limit: int | None = None, tail: bool = False) -> bytes | None:
    """The regular file at `path`: whole, else its first `limit` bytes (its last with `tail`). None for a symlink,
    anything but a regular file, or any OSError."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None
            if limit is None:
                return f.read()
            if tail:
                f.seek(max(f.seek(0, os.SEEK_END) - limit, 0))
            return f.read(limit)
    except OSError:
        return None
