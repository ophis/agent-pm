"""Subprocess runs with a timeout, their stderr shown with URL userinfo masked."""

import re
import subprocess
from collections.abc import Callable

SHORT, LONG = 60, 600
USERINFO = re.compile(r"//[^@/\s]+@")

Runner = Callable[[list[str], int], subprocess.CompletedProcess]


class Stopped(BaseException):
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def sh(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
    )


def redact(text: str) -> str:
    return USERINFO.sub("//***@", text)


def err_text(res: subprocess.CompletedProcess) -> str:
    return redact((res.stderr or "").strip())[:200]
