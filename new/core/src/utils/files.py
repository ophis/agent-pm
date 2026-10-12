import os
import stat


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
