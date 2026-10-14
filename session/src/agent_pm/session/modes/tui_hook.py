"""Each tui hook runs `<python> -I tui_hook.py <session dir> <roots JSON> <label>` with JSON on stdin. Checks
session.json, runs its optional adapter_hook to bind runtime identity, appends {"ts", "label",
"payload"} to hooks.jsonl, then marks the pane with the label's word (tmux_host.mark) using
session.json's tmux_path, never PATH. Payload strs are cut to CUT chars unless the word is done.
The entry script uses only the stdlib; adapter callbacks import packages after the supplied roots
are set. Never stdout, always exits 0. Failures append a full traceback to hook.log
when writable and report one stderr line.
"""

from __future__ import annotations

import fcntl
import importlib
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import sys
import traceback
from contextlib import suppress
from datetime import datetime
from enum import StrEnum


class HookError(Exception):
    """One stderr line; the hook stops."""


PAYLOAD_MAX = 64 << 20  # bytes of stdin parsed

STATE_MAX = 1 << 20  # bytes of session.json read

BLOCK = 1 << 16

CUT = 4096

LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class Word(StrEnum):
    DONE = "done"
    BLOCKED = "blocked"
    WORKING = "working"


STATE = "session.json"

HOOKS = "hooks.jsonl"

TMUX_HOST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hosts/tmux/tmux_host.py"
)


def _abs(value) -> bool:
    return isinstance(value, str) and value.isprintable() and os.path.isabs(value)


SESSION_STATE = {  # what this hook or an adapter_hook reads
    "words": lambda v: (
        isinstance(v, dict) and all(LABEL.fullmatch(k) and w in tuple(Word) for k, w in v.items())
    ),
    "events": lambda v: v is None or _abs(v),
    "sid": lambda v: isinstance(v, str) and v != "" and v.isprintable() and "/" not in v,
    "workdir": _abs,
    "tmux_path": _abs,
}


def main(argv: list[str], stdin, *, load_host=None) -> None:
    payload = read_payload(stdin)
    if len(argv) != 3 or not _abs(argv[0]) or not LABEL.fullmatch(argv[2]):
        raise HookError(
            "usage: tui_hook.py <absolute session dir> <roots JSON> <label [A-Za-z0-9_.-]{1,64}>"
        )
    folder, encoded_roots, label = argv
    roots = _source_roots(encoded_roots)
    state = read_state(os.path.join(folder, STATE))
    sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]
    if "adapter_hook" in state:
        adapter = load_adapter(state["adapter_hook"], roots)
        if adapter.on_hook(label, payload, state) is False:
            return
    word = state["words"].get(label)
    if word != Word.DONE:
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


def _source_roots(encoded: str) -> list[str]:
    # This runs before package imports, when only the standard library is available.
    try:
        roots = json.loads(encoded)
    except (ValueError, RecursionError):
        raise HookError(
            "invalid source roots: expected a nonempty JSON array of directories"
        ) from None
    if (
        not isinstance(roots, list)
        or not roots
        or any(not _abs(path) or not os.path.isdir(path) for path in roots)
        or len(set(roots)) != len(roots)
    ):
        raise HookError("invalid source roots: expected distinct absolute directories")
    return roots


def read_payload(stream) -> dict:
    """stdin's JSON object, read to its end (the writer never stalls); {} when over PAYLOAD_MAX bytes
    or not one.
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
        say(f"payload: over {PAYLOAD_MAX} bytes")
        return {}
    try:
        payload = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        payload = None
    if not isinstance(payload, dict):
        say("payload: not a JSON object")
        return {}
    return payload


def read_state(path: str, *, error: type[Exception] = HookError) -> dict:
    """session.json: a regular file (never followed), at most STATE_MAX bytes, its fields passing
    SESSION_STATE; else `error`.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(
                os.fstat(fd).st_mode
            ):  # first: fdopen raises on a directory, leaving fd open
                raise error(f"{path}: not a regular file")
            with os.fdopen(fd, "rb", closefd=False) as f:
                data = f.read(STATE_MAX + 1)
        finally:
            os.close(fd)
    except OSError as e:
        raise error(f"{path}: {e.strerror}") from None
    if len(data) > STATE_MAX:
        raise error(f"{path}: over {STATE_MAX} bytes")
    try:
        state = json.loads(data)
    except (ValueError, RecursionError):
        state = None
    if not isinstance(state, dict):
        raise error(f"{path}: not a JSON object")
    for field, check in SESSION_STATE.items():
        if field not in state or not check(state[field]):
            raise error(f"{path}: bad {field}")
    if "adapter_hook" in state and (
        not isinstance(state["adapter_hook"], str)
        or not all(part.isidentifier() for part in state["adapter_hook"].split("."))
    ):
        raise error(f"{path}: bad adapter_hook")
    if "adapter_state" in state and not isinstance(state["adapter_state"], dict):
        raise error(f"{path}: bad adapter_state")
    return state


def load_adapter(name: str, roots: list[str]):
    paths = sys.path
    parts = name.split(".")
    for index in range(len(parts)):
        # A segment lookup needs no loaded parent, including for namespace packages.
        spec = importlib.machinery.PathFinder.find_spec(parts[index], paths)
        if spec is None or (spec.origin is None and index == len(parts) - 1):
            raise HookError(f"adapter hook {name}: cannot find Python module")
        if spec.origin is not None:
            if not any(
                os.path.commonpath((root, spec.origin)) == os.path.normpath(root) for root in roots
            ):
                raise HookError(f"adapter hook {name}: module outside source roots")
            try:
                info = os.lstat(spec.origin)
            except OSError as error:
                raise HookError(f"adapter hook {name}: {error.strerror}") from error
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()):
                raise HookError(f"adapter hook {name}: not a regular file owned by you or root")
        paths = list(spec.submodule_search_locations or ())
    module = importlib.import_module(name)
    if not callable(getattr(module, "on_hook", None)):
        raise HookError(f"adapter hook {name}: missing on_hook callback")
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
    spec = importlib.util.spec_from_file_location("tmux_host", TMUX_HOST)
    if spec is None or spec.loader is None:
        raise HookError(f"{TMUX_HOST}: cannot load Python module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def say(text: str) -> None:
    """One stderr line; none when stderr is closed (print would fall back to stdout)."""
    if sys.stderr is not None:
        print("tui_hook: " + " ".join(text.split()), file=sys.stderr)


if __name__ == "__main__":
    try:
        main(sys.argv[1:], sys.stdin.buffer)
    except Exception as e:  # noqa: BLE001  # the hook must never fail the client runtime
        if len(sys.argv) > 1 and _abs(sys.argv[1]):
            with (
                suppress(OSError),
                open(
                    os.path.join(sys.argv[1], "hook.log"),
                    "a",
                    encoding="utf-8",
                    errors="backslashreplace",
                ) as file,
            ):
                file.write(traceback.format_exc())
        say(str(e) if isinstance(e, HookError) else f"{type(e).__name__}: {e}")
    sys.exit(0)
