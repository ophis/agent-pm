"""A dead harness's callback, as callback.sweep sees it, without a process: a held callback lock and
an os.killpg fake; a clock whose time.sleep advances time.monotonic.
"""

import fcntl
import os
import signal
import unittest
from unittest import mock


class Clock:
    def __init__(self, case: unittest.TestCase):
        self.now = 0.0
        case.enterContext(mock.patch("time.monotonic", side_effect=lambda: self.now))
        case.enterContext(mock.patch("time.sleep", side_effect=self.advance))

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Orphan:
    """A callback lock `text` held by another open file, and an os.killpg fake over its group: the
    group ends on SIGKILL when `dies` (or is gone once `alive` is false), freeing the lock unless
    `keeps`; a 0 probe raises `probe_error` when set.
    """

    def __init__(self, case: unittest.TestCase, lock: str, text: str):
        with open(lock, "w", encoding="utf-8") as file:
            file.write(text)
        self.holder: int | None = os.open(lock, os.O_RDONLY)
        fcntl.flock(self.holder, fcntl.LOCK_EX)
        case.addCleanup(self.free)
        self.calls: list = []
        self.alive, self.dies, self.keeps = True, True, False
        self.probe_error: BaseException | None = None

    def free(self):
        if self.holder is not None:
            os.close(self.holder)
            self.holder = None

    def killpg(self, pid, signum):
        self.calls.append((pid, signum))
        if signum == 0 and self.probe_error is not None:
            raise self.probe_error
        if signum == signal.SIGKILL and self.dies:
            self.alive = False
        if not self.alive:
            if not self.keeps:
                self.free()
            if signum == 0:
                raise ProcessLookupError(3, "No such process")
