"""Private runtime copies owned by supervised processes, with abandoned-copy collection."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from typing import TypeVar, cast

HANDOFF = 60
IGNORED = shutil.ignore_patterns(
    "tests",
    "__pycache__",
    ".git",
    ".autopilot",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    "*.pyc",
)
T = TypeVar("T")


def installations_root() -> str:
    return os.path.expanduser("~/.agent-pm/installations")


def relocate(value: T, source: str, destination: str) -> T:
    if isinstance(value, str):
        if value.startswith(("{", "[")):
            with contextlib.suppress(ValueError, RecursionError):
                return cast(T, json.dumps(relocate(json.loads(value), source, destination)))
        return cast(
            T, re.sub(re.escape(source) + r"(?=$|[/\s\"':,;\]])", lambda _match: destination, value)
        )
    if isinstance(value, dict):
        return cast(T, {key: relocate(item, source, destination) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return cast(T, type(value)(relocate(item, source, destination) for item in value))
    return value


def runtime_paths(root: str) -> tuple[str, ...]:
    paths = []
    for entry in os.scandir(root):
        if entry.name.startswith(".") or entry.name in ("tests",):
            continue
        if entry.is_file() or entry.name in ("bin", "docs"):
            paths.append(entry.name)
        elif os.path.isdir(os.path.join(entry.path, "src")):
            paths.extend(
                os.path.join(entry.name, child.name)
                for child in os.scandir(entry.path)
                if child.name not in IGNORED(entry.path, [child.name])
            )
    return tuple(paths)


def relocate_runtime(value: T, source: str, destination: str) -> T:
    existing = source if os.path.isdir(source) else destination
    for part in runtime_paths(existing):
        value = relocate(value, os.path.join(source, part), os.path.join(destination, part))
    return value


def _private(path: str, *, create: bool = False) -> None:
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(path, 0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"not an owned private directory: {path}")


def _lease(folder: str) -> int:
    descriptor = os.open(os.path.join(folder, ".lease"), os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(descriptor)
        raise ValueError(f"not an owned regular lease: {folder}")
    return descriptor


def retire(folder: str, *, wait: bool = False) -> bool:
    _private(folder)
    descriptor = _lease(folder)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            return False
        shutil.rmtree(folder)
        return True
    finally:
        os.close(descriptor)


def sweep(root: str, *, now: float | None = None) -> None:
    current = time.time() if now is None else now
    for entry in os.scandir(root):
        if not entry.name.startswith(("run-", ".copy-")) or not entry.is_dir(follow_symlinks=False):
            continue
        with contextlib.suppress(OSError, ValueError):
            if current - entry.stat(follow_symlinks=False).st_mtime < HANDOFF:
                continue
            retire(entry.path)


def _copy(source: str, destination: str) -> None:
    def ignore(folder: str, names: list[str]) -> set[str]:
        ignored = set(IGNORED(folder, names))
        for name in set(names) - ignored:
            path = os.path.join(folder, name)
            if (
                os.path.islink(path)
                and os.path.commonpath((source, os.path.realpath(path))) != source
            ):
                raise ValueError(f"symlink outside installation: {path}")
        return ignored

    parts = [
        entry.name
        for entry in os.scandir(source)
        if not entry.name.startswith(".")
        and (
            entry.is_file()
            or entry.name in ("bin", "docs")
            or os.path.isdir(os.path.join(entry.path, "src"))
        )
    ]
    for part in parts:
        path, target = os.path.join(source, part), os.path.join(destination, part)
        if os.path.islink(path) and os.path.commonpath((source, os.path.realpath(path))) != source:
            raise ValueError(f"symlink outside installation: {path}")
        if os.path.isdir(path):
            shutil.copytree(path, target, ignore=ignore)
            for folder, _dirs, _files in os.walk(target):
                os.chmod(folder, os.stat(folder).st_mode | stat.S_IWUSR)
        elif os.path.isfile(path):
            shutil.copy2(path, target)


class Installation:
    def __init__(self, source: str, *, root: str | None = None):
        self.source = os.path.abspath(source)
        parent = installations_root() if root is None else root
        state = os.path.dirname(parent)
        if not os.path.lexists(state):
            os.mkdir(state, 0o700)
        if (
            os.path.islink(state)
            or os.stat(state).st_uid != os.getuid()
            or os.stat(state).st_mode & 0o022
        ):
            raise ValueError(f"not an owned private directory: {state}")
        _private(parent, create=True)
        sweep(parent)
        folder = tempfile.mkdtemp(dir=parent, prefix=".copy-")
        self.root, self._descriptor, self._handed = folder, -1, False
        try:
            self._descriptor = os.open(
                os.path.join(folder, ".lease"), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
            )
            fcntl.flock(self._descriptor, fcntl.LOCK_SH)
            _copy(os.path.realpath(self.source), folder)
            published = os.path.join(
                parent, "run-" + os.path.basename(folder).removeprefix(".copy-")
            )
            os.rename(folder, published)
            self.root = published
        except BaseException:
            if self._descriptor >= 0:
                os.close(self._descriptor)
                self._descriptor = -1
            shutil.rmtree(folder)
            raise

    def __enter__(self):
        return self

    def __exit__(self, kind, _value, _traceback):
        if kind is not None or not self._handed:
            self.discard()
        elif self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1

    def discard(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1
        if os.path.exists(self.root):
            with contextlib.suppress(OSError, ValueError):
                retire(self.root)

    def relocate(self, value: T) -> T:
        for source in dict.fromkeys((self.source, os.path.realpath(self.source))):
            value = relocate_runtime(value, source, self.root)
        return value

    def environment(self, env: Mapping[str, str]) -> dict[str, str]:
        mapped = self.relocate(dict(env))
        mapped["PATH"] = (
            self.root + "/bin" + os.pathsep + mapped.get("PATH", os.environ.get("PATH", os.defpath))
        )
        return mapped

    def command(self, argv: Sequence[str]) -> tuple[str, ...]:
        self._handed = True
        script = os.path.join(self.root, "utils/src/agent_pm/utils/installation.py")
        return (
            sys.executable,
            "-P",
            "-s",
            script,
            self.root,
            json.dumps(self.relocate(tuple(argv))),
        )


def supervise(folder: str, argv: Sequence[str]) -> int:
    _private(folder)
    descriptor = _lease(folder)
    fcntl.flock(descriptor, fcntl.LOCK_SH)
    child = None
    previous = {}
    signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, ())
    pending = []

    def forward(signum, _frame):
        if child is None:
            pending.append(signum)
        else:
            with contextlib.suppress(ProcessLookupError):
                child.send_signal(signum)

    try:
        for signum in signals:
            previous[signum] = signal.signal(signum, forward)
        child = subprocess.Popen(argv, pass_fds=(descriptor,))
        for signum in pending:
            forward(signum, None)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, signals)
        code = child.wait()
        return 128 - code if code < 0 else code
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        os.close(descriptor)
        with contextlib.suppress(OSError, ValueError):
            retire(folder, wait=True)
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)


if __name__ == "__main__":
    sys.exit(supervise(sys.argv[1], json.loads(sys.argv[2])))
