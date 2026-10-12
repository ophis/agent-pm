"""GUARD: git options that keep a checkout's fsmonitor and hooks from running; common_dir: a worktree's clone; git:
runs git, retrying lock failures."""

import os
import re
import time

from utils.shell import SHORT, Runner, err_text

GUARD = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null")
LOCK = re.compile(r"(?:cannot|could not) lock|\.lock\b", re.I)


def git(run: Runner, wt: str, *args: str, timeout: int = SHORT, pre: tuple[str, ...] = ()) -> str:
    for i in range(5):
        res = run(["git", *pre, "-C", wt, *args], timeout)
        if res.returncode == 0:
            return res.stdout
        if i == 4 or not LOCK.search(res.stderr or ""):
            break
        time.sleep(0.5 * 2**i)
    shown = " ".join(a if len(a) <= 40 else a[:37] + "..." for a in args)
    raise RuntimeError(f"git {shown[:200]}: {err_text(res)}")


def common_dir(wt: str) -> str | None:
    # never runs git: reads the files as git does (whole file, trailing CR/LF dropped)
    def read(path):
        if not os.path.isfile(path):  # a FIFO would block open()
            return None
        with open(path, "rb") as f:
            return f.read(4096).decode().rstrip("\r\n")

    try:
        dotgit = os.path.join(wt, ".git")
        text = None if os.path.islink(dotgit) else read(dotgit)
        if not text or not text.startswith("gitdir: "):
            return None
        gitdir = os.path.realpath(os.path.join(wt, text[len("gitdir: ") :]))
        rel = read(os.path.join(gitdir, "commondir"))
        common = os.path.realpath(os.path.join(gitdir, rel)) if rel else None
    except (OSError, ValueError):  # UnicodeDecodeError and a NUL in a path are ValueErrors
        return None
    return (
        common if common and os.path.basename(common) == ".git" and os.path.isdir(common) else None
    )
