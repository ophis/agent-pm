"""DockerPiped, a headless client's `docker exec -i` (DockerHost.pipe): its lines capped, as the
container's output is untrusted; close() removes its container once. Token refresh for a piped
generation belongs here.
"""

import os
import threading
from collections.abc import Callable

from agent_pm.session.engine.types import SessionError
from agent_pm.session.hosts.base import LocalPiped
from agent_pm.session.hosts.docker.daemon import OUT_MAX

LINE_MAX = OUT_MAX


class _Capped:
    def __init__(self, name: str, pipe) -> None:
        self.name, self.pipe = name, pipe

    def __iter__(self):
        while line := self.pipe.readline(LINE_MAX + 1):
            if len(line) > LINE_MAX and not line.endswith("\n"):
                raise SessionError(
                    f"docker session {self.name}: a client output line over {LINE_MAX} characters"
                )
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *_) -> None:
        self.pipe.close()


class DockerPiped(LocalPiped):
    """lock: the pipe lock's fd, held while it runs; close: what frees its container."""

    def __init__(self, name: str, proc, *, close: Callable[[], None], lock: int) -> None:
        super().__init__(proc)
        self.stdout, self.stderr = _Capped(name, self.stdout), _Capped(name, self.stderr)
        self._close, self._lock = close, lock
        self._once, self._closed = threading.Lock(), False

    def close(self) -> None:
        with self._once:
            if self._closed:
                return
            self._closed = True
            try:
                self._close()
            finally:
                os.close(self._lock)
