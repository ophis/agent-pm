"""The State dir: each Delegate run's authoritative state, outside its agent-writable workdir.

`~/.agent-pm/delegates/` (0700) holds, per workdir key (the sha256 of the workdir's absolute path,
symlinks not resolved): `<key>.json`, the state record (the authoritative `delegate.json` bytes);
`<key>.lock`, the mutex; `<key>.run.lock`, the run lock a running Delegate holds.
`<workdir>/delegate.json` is the state record's published copy.
"""

import contextlib
import fcntl
import hashlib
import os
import stat
from collections.abc import Iterator

from agent_pm.delegate.engine.types import ConfigError
from agent_pm.utils import STATE, file_lock, read_regular, save

DIR = f"{STATE}/delegates"
PUBLISHED = "delegate.json"


class RunState:
    """The State dir's files for one workdir. Its OSErrors propagate."""

    def __init__(self, workdir: str) -> None:
        self.workdir = os.path.abspath(workdir)
        self.dir = os.path.expanduser(DIR)
        self.base = os.path.join(self.dir, hashlib.sha256(os.fsencode(self.workdir)).hexdigest())
        self.record = f"{self.base}.json"
        self.published = os.path.join(self.workdir, PUBLISHED)

    def read(self) -> bytes | None:
        return read_regular(self.record, own=True)

    def write(self, payload: str) -> None:
        """The state record, then its published copy (making the workdir)."""
        self._made()
        for path in (self.record, self.published):
            save(path, payload, own=True, durable=True)

    @contextlib.contextmanager
    def mutex(self) -> Iterator[None]:
        self._made()
        with file_lock(self.base, own=True):
            yield

    @contextlib.contextmanager
    def run_lock(self) -> Iterator[None]:
        self._made()
        with file_lock(f"{self.base}.run", own=True):
            yield

    def running(self) -> bool:
        """Whether a run holds the run lock."""
        try:
            descriptor = os.open(
                f"{self.base}.run.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
        except FileNotFoundError:
            return False
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
                raise ConfigError(f"{self.workdir}: invalid delegate run lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            return False
        finally:
            os.close(descriptor)

    def _made(self) -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
