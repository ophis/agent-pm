"""Standalone unified runtime hook: bounded payload, adapter binding, journal and tmux marking."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import runpy
import stat
import sys
from collections.abc import Callable
from datetime import datetime
from logging import ERROR, Handler, Logger, LogRecord
from types import SimpleNamespace


class HookError(Exception):
    """One stderr line; the hook stops."""


class EventHandler(Handler):
    def __init__(self, deliver: Callable[[BaseException], None]):
        super().__init__()
        self.deliver = deliver

    def emit(self, record: LogRecord) -> None:
        if record.exc_info and record.exc_info[1] is not None:
            self.deliver(record.exc_info[1])


class FailureReport:
    def __init__(self, name: str, error: BaseException, receive: Callable[[BaseException], None]):
        self.error, self.receive, self.delivered = error, receive, False
        self.logger = Logger(name, ERROR)
        self.logger.propagate = False
        self.handler = EventHandler(self._deliver)

    def _deliver(self, error: BaseException) -> None:
        self.receive(error)
        self.delivered = True

    def __enter__(self) -> Logger:
        self.logger.addHandler(self.handler)
        return self.logger

    def __exit__(self, kind, _value, _traceback) -> None:
        try:
            if kind is None and not self.delivered:
                self.receive(self.error)
        finally:
            self.logger.removeHandler(self.handler)
            self.handler.close()


PAYLOAD_MAX = 64 << 20  # bytes of stdin parsed

STATE_MAX = 1 << 20  # bytes of session.json read

BLOCK = 1 << 16

CUT = 4096

LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")

RUNTIME = re.compile(r"[a-z][a-z0-9_]*")

WORDS = ("done", "blocked", "working")

STATE = "session.json"

HOOKS = "hooks.jsonl"

TMUX_HOST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hosts/tmux/tmux_host.py"
)


def _abs(value) -> bool:
    return isinstance(value, str) and value.isprintable() and os.path.isabs(value)


SESSION_STATE = {
    "words": lambda v: (
        isinstance(v, dict) and all(LABEL.fullmatch(k) and w in WORDS for k, w in v.items())
    ),
    "events": lambda v: v is None or _abs(v),
    "sid": lambda v: isinstance(v, str) and v != "" and v.isprintable() and "/" not in v,
    "runtime": lambda v: isinstance(v, str) and RUNTIME.fullmatch(v) is not None,
    "workdir": _abs,
    "cwd": lambda v: v == "" or _abs(v),
    "transcript": lambda v: v == "" or _abs(v),
    "project": lambda v: isinstance(v, bool),
    "resume": lambda v: isinstance(v, str),
    "tmux_path": _abs,
}


def main(argv: list[str], stdin, *, load_host=None) -> None:
    payload = read_payload(stdin)
    if len(argv) != 2 or not _abs(argv[0]) or not LABEL.fullmatch(argv[1]):
        raise HookError("usage: tui_hook.py <absolute session dir> <label [A-Za-z0-9_.-]{1,64}>")
    folder, label = argv
    state = read_state(os.path.join(folder, STATE))
    if "adapter_hook" in state:
        adapter = load_adapter(state["adapter_hook"])
        if adapter.on_hook(label, payload, state) is False:
            return
    word = state["words"].get(label)
    if word != "done":
        payload = cut(payload)
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    append(os.path.join(folder, HOOKS), {"ts": stamp, "label": label, "payload": payload})
    if word:
        tmux = check_tmux(state["tmux_path"])
        tmux_host = (load_tmux_host if load_host is None else load_host)()
        try:
            tmux_host.mark(word, state["events"], tmux=tmux)
        except tmux_host.TuiError as e:
            raise HookError(str(e)) from None


def read_payload(stream) -> dict:
    """stdin's JSON object, read to its end (the writer never stalls); {} when not one; overflow
    raises HookError.
    """
    chunks, size = [], 0
    while True:
        chunk = stream.read(BLOCK)
        if not chunk:
            break
        size += len(chunk)
        if size <= PAYLOAD_MAX:
            chunks.append(chunk)
    if size > PAYLOAD_MAX:
        raise HookError(f"payload: over {PAYLOAD_MAX} bytes")
    try:
        payload = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        payload = None
    if not isinstance(payload, dict):
        say("payload: not a JSON object")
        return {}
    return payload


def read_state(path: str) -> dict:
    """session.json: a regular file (never followed), at most STATE_MAX bytes, its fields passing
    SESSION_STATE; else HookError.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(
                os.fstat(fd).st_mode
            ):  # first: fdopen raises on a directory, leaving fd open
                raise HookError(f"{path}: not a regular file")
            with os.fdopen(fd, "rb", closefd=False) as f:
                data = f.read(STATE_MAX + 1)
        finally:
            os.close(fd)
    except OSError as e:
        raise HookError(f"{path}: {e.strerror}") from None
    if len(data) > STATE_MAX:
        raise HookError(f"{path}: over {STATE_MAX} bytes")
    try:
        state = json.loads(data)
    except (ValueError, RecursionError):
        state = None
    if not isinstance(state, dict):
        raise HookError(f"{path}: not a JSON object")
    for field, check in SESSION_STATE.items():
        if field not in state or not check(state[field]):
            raise HookError(f"{path}: bad {field}")
    if "adapter_hook" in state and not _abs(state["adapter_hook"]):
        raise HookError(f"{path}: bad {'adapter_hook'}")
    if "adapter_state" in state and not isinstance(state["adapter_state"], dict):
        raise HookError(f"{path}: bad {'adapter_state'}")
    return state


def load_adapter(path: str):
    try:
        info = os.lstat(path)
    except OSError as error:
        raise HookError(f"adapter hook {path}: {error.strerror}") from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()):
        raise HookError(f"adapter hook {path}: not a regular file owned by you or root")
    spec = importlib.util.spec_from_file_location("_agent_pm_adapter_hook", path)
    if spec is None or spec.loader is None:
        raise HookError(f"adapter hook {path}: cannot load Python module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "on_hook", None)):
        raise HookError(f"adapter hook {path}: missing on_hook callback")
    return module


def check_tmux(path: str) -> str:
    try:
        st = os.stat(path)
    except OSError as e:
        raise HookError(f"tmux {path}: {e.strerror}") from None
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid not in (0, os.getuid())
        or not os.access(path, os.X_OK)
    ):
        raise HookError(f"tmux {path}: not a regular executable file owned by you or root")
    return path


def cut(value):
    if isinstance(value, str):
        return value[:CUT]
    if isinstance(value, list):
        return [cut(v) for v in value]
    if isinstance(value, dict):
        return {k: cut(v) for k, v in value.items()}
    return value


def append(path: str, record: dict) -> None:
    """One write under an exclusive flock to a regular file, created 0600, never followed."""
    line = (json.dumps(record) + "\n").encode()
    try:
        # O_NONBLOCK: a FIFO put there fails instead of hanging the hook
        fd = os.open(
            path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise HookError(f"{path}: not a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            written = os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as e:
        raise HookError(f"{path}: {e.strerror}") from None
    if written != len(line):
        raise HookError(f"{path}: short write")


def load_tmux_host():
    """Load the host by path, without changing the search path or caching the host module."""
    return SimpleNamespace(**runpy.run_path(TMUX_HOST))


def say(text: str) -> None:
    """One stderr line; none when stderr is closed (print would fall back to stdout)."""
    if sys.stderr is not None:
        print("tui_hook: " + " ".join(text.split()), file=sys.stderr)


def report_error(error: BaseException) -> None:
    say(str(error) if isinstance(error, HookError) else f"{type(error).__name__}: {error}")


if __name__ == "__main__":
    try:
        main(sys.argv[1:], sys.stdin.buffer)
    except Exception as e:
        with FailureReport("session.tui_hook", e, report_error) as logger:
            logger.exception("Hook execution failed")
    sys.exit(0)
