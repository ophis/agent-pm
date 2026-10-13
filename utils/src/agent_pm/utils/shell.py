"""Subprocess runs with a timeout, their stderr shown with URL userinfo masked."""

import re
import shlex
import subprocess
from collections.abc import Callable, Mapping

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


def resume_command(cwd: str, env: Mapping[str, str], argv) -> str:
    assign = "".join(f"{name}={shlex.quote(value)} " for name, value in env.items())
    return f"cd {shlex.quote(cwd)} && {assign}{shlex.join(argv)}"
