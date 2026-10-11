"""The command each hook of a tui session runs: `<python> -I tui_hook.py <session dir> <label>`, the hook's JSON payload
on stdin. Checks the dir's session.json (SESSION_STATE), appends one hook record {"ts", "label", "payload"} to its
hooks.jsonl, then marks the pane with the label's word (tmux_host.mark: done and blocked lines only when the events
file is set; a label with no word marks nothing). The reading process maps records to events through its adapter. Each
payload str is cut to CUT chars unless the label's word is done. Never prints on stdout and always exits 0 (a runtime
may read a hook's stdout or exit status); each error is one stderr line. Python 3.9, stdlib only, standalone:
driver/tmux_host.py is loaded by its path, nothing joins sys.path."""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import stat
import sys
from datetime import datetime

PAYLOAD_MAX = 16 << 20   # bytes of stdin parsed
STATE_MAX = 1 << 20      # bytes of session.json read
BLOCK = 1 << 16
CUT = 4096
LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")
RUNTIME = re.compile(r"[a-z][a-z0-9_]*")
WORDS = ("done", "blocked", "working")
STATE = "session.json"
HOOKS = "hooks.jsonl"
TMUX_HOST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tmux_host.py")


def _abs(value) -> bool:
    return isinstance(value, str) and value.isprintable() and os.path.isabs(value)


SESSION_STATE = {
    "words": lambda v: isinstance(v, dict) and all(LABEL.fullmatch(k) and w in WORDS for k, w in v.items()),
    "events": lambda v: v is None or _abs(v),
    "sid": lambda v: isinstance(v, str) and v != "" and v.isprintable() and "/" not in v,
    "runtime": lambda v: isinstance(v, str) and RUNTIME.fullmatch(v) is not None,
    "workdir": _abs,
    "cwd": lambda v: v == "" or _abs(v),
    "transcript": lambda v: v == "" or _abs(v),
    "project": lambda v: isinstance(v, bool),
    "resume": lambda v: isinstance(v, str),
}


class HookError(Exception):
    """One stderr line; the hook stops."""


def main(argv: list[str], stdin) -> None:
    payload = read_payload(stdin)
    if len(argv) != 2 or not _abs(argv[0]) or not LABEL.fullmatch(argv[1]):
        raise HookError("usage: tui_hook.py <absolute session dir> <label [A-Za-z0-9_.-]{1,64}>")
    folder, label = argv
    state = read_state(os.path.join(folder, STATE))
    word = state["words"].get(label)
    if word != "done":
        payload = cut(payload)
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    append(os.path.join(folder, HOOKS), {"ts": stamp, "label": label, "payload": payload})
    if word:
        tmux_host = load_tmux_host()
        try:
            tmux_host.mark(word, state["events"])
        except tmux_host.TuiError as e:
            raise HookError(str(e)) from None


def read_payload(stream) -> dict:
    """stdin's JSON object, read to its end (the writer never stalls); {} and a stderr line when it is over PAYLOAD_MAX
    bytes or not one."""
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


def read_state(path: str) -> dict:
    """session.json, a regular file (never followed) holding a JSON object of at most STATE_MAX bytes whose fields pass
    SESSION_STATE (any same-user process may have changed it); else HookError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):   # first: fdopen raises on a directory, leaving fd open
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
    return state


def cut(value):
    """value with each str in it (dict keys aside) cut to CUT chars."""
    if isinstance(value, str):
        return value[:CUT]
    if isinstance(value, list):
        return [cut(v) for v in value]
    if isinstance(value, dict):
        return {k: cut(v) for k, v in value.items()}
    return value


def append(path: str, record: dict) -> None:
    """record as one line, in one write under an exclusive flock, to path: a regular file, created 0600, never
    followed."""
    line = (json.dumps(record) + "\n").encode()
    try:
        # O_NONBLOCK: a FIFO put there fails instead of hanging the hook
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
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
    """driver/tmux_host.py loaded by its path: nothing joins sys.path or sys.modules."""
    spec = importlib.util.spec_from_file_location("tmux_host", TMUX_HOST)
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
    except Exception as e:
        say(str(e) if isinstance(e, HookError) else f"{type(e).__name__}: {e}")
    sys.exit(0)
