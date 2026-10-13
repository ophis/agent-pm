"""Roster event files: complete lines, cursors and rotation with in-flight append settlement."""

import os
import sys
import time

from agent_pm.utils import append_line, read_regular

EVENTS_MAX = 1 << 20
SETTLE = 1


class Events:
    def __init__(self, directory: str):
        self.path = os.path.join(directory, "events")

    def ensure(self) -> None:
        append_line(self.path, "", own=True)

    def read(self) -> bytes:
        data = read_regular(self.path, own=True)
        if data is None:
            if os.path.lexists(self.path):
                raise OSError(f"{self.path}: not a readable regular file owned by you")
            return b""
        return data

    def following(self, after: int) -> list[str]:
        data = self.read().split(b"\n")[:-1]
        return [line.decode(errors="replace") for line in data[after:]]

    def count(self) -> int:
        return self.read().count(b"\n")

    def _due(self, data: bytes, cursor: int) -> bool:
        retained = data.split(b"\n", cursor)[-1]
        return len(data) > EVENTS_MAX and len(data) - len(retained) >= EVENTS_MAX // 2

    def due(self, cursor: int) -> bool:
        return self._due(self.read(), cursor)

    def rotate(self, cursor: int) -> bool:
        if not self.due(cursor):
            return False
        old = self.path + ".1"
        if os.path.lexists(old) and read_regular(old, own=True) is None:
            raise OSError(f"{old}: not a readable regular file owned by you")
        os.replace(self.path, old)
        try:
            self.ensure()
            time.sleep(SETTLE)
            data = read_regular(old, own=True)
            if data is None:
                raise OSError(f"{old}: not a readable regular file owned by you")
            tail = data.split(b"\n", cursor)[-1]
            if tail and not tail.endswith(b"\n"):
                tail += b"\n"
            if tail:
                append_line(self.path, tail, own=True, durable=True)
        except OSError as error:
            print(
                f"manager: rotate: {error}: unread lines after {cursor} retained in {old}",
                file=sys.stderr,
            )
        return True
