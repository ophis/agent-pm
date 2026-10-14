"""A standalone tui hook drains stdin, checks callback sources, binds client identity,
and appends one hooks.jsonl record before notifying its optional Host callback.
Never stdout, always exits 0; failures retain a traceback and one stderr line.
session.json's adapter_hook and host_hook are HookCallback.fields(): a callback module is
trusted under the given source roots or its own declared roots, each its on_hook given a HookCall.
"""

from __future__ import annotations

import fcntl
import importlib
import importlib.machinery
import json
import os
import re
import stat
import sys
import traceback
from contextlib import suppress
from datetime import datetime

PAYLOAD_MAX = 64 << 20  # bytes of stdin parsed

STATE_MAX = 1 << 20  # bytes of session.json read

BLOCK = 1 << 16

CUT = 4096

LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")


STATE = "session.json"

HOOKS = "hooks.jsonl"


def _abs(value) -> bool:
    return isinstance(value, str) and value.isprintable() and os.path.isabs(value)


def _source_roots(encoded: str) -> list[str]:
    # This runs before package imports, when only the standard library is available.
    try:
        roots = json.loads(encoded)
    except (ValueError, RecursionError):
        raise ValueError(
            "invalid source roots: expected a nonempty JSON array of directories"
        ) from None
    if (
        not isinstance(roots, list)
        or not roots
        or any(not _abs(path) or not os.path.isdir(path) for path in roots)
        or len(set(roots)) != len(roots)
    ):
        raise ValueError("invalid source roots: expected distinct absolute directories")
    return roots


def _arguments(argv: list[str]) -> tuple[str, list[str], str]:
    if len(argv) != 3 or not _abs(argv[0]) or not LABEL.fullmatch(argv[2]):
        raise ValueError(
            "usage: tui_hook.py <absolute session dir> <roots JSON> <label [A-Za-z0-9_.-]{1,64}>"
        )
    folder, encoded_roots, label = argv
    return folder, _source_roots(encoded_roots), label


def _bootstrap(roots: list[str]) -> None:
    sys.path[:] = [*roots, *(path for path in sys.path if path not in roots)]


def say(text: str) -> None:
    """One stderr line; none when stderr is closed (print would fall back to stdout)."""
    if sys.stderr is not None:
        print("tui_hook: " + " ".join(text.split()), file=sys.stderr)


if __name__ != "__main__":
    from agent_pm.session.engine.types import HookCall, HookError, Word


SESSION_STATE = {  # what this hook or an adapter_hook reads
    "words": lambda v: (
        isinstance(v, dict) and all(LABEL.fullmatch(k) and w in tuple(Word) for k, w in v.items())
    ),
    "events": lambda v: v is None or _abs(v),
    "sid": lambda v: isinstance(v, str) and v != "" and v.isprintable() and "/" not in v,
    "workdir": _abs,
}


def _prepare(argv: list[str]):
    folder, roots, label = _arguments(argv)
    path = os.path.join(folder, STATE)
    state = _read_state(path, ValueError)
    _check_callbacks(state, path, ValueError)
    hooks = [
        (kind, state[kind + "_hook"]) for kind in ("adapter", "host") if kind + "_hook" in state
    ]
    for kind, hook in hooks:
        if missing := [root for root in hook["roots"] if not os.path.isdir(root)]:
            raise ValueError(f"{kind} hook {hook['module']}: root {missing[0]} is no directory")
    own = [root for _, hook in hooks for root in hook["roots"] if root not in roots]
    _bootstrap([*roots, *dict.fromkeys(own)])
    for kind, hook in hooks:
        _check_module(hook["module"], [*roots, *hook["roots"]], kind + " hook", ValueError)
    _check_module("agent_pm.session.engine.types", roots, "shared types", ValueError)
    return folder, label, state


def main(argv: list[str], stdin) -> None:
    payload = read_payload(stdin)
    try:
        folder, label, state = _prepare(argv)
    except ValueError as error:
        raise HookError(str(error)) from None
    _dispatch(folder, label, payload, state)


def _dispatch(folder, label, payload, state):
    _validate_state(state, os.path.join(folder, STATE), HookError)

    def call(kind, given) -> HookCall:
        return HookCall(
            label, given, state["sid"], state["workdir"], state["events"], state[kind]["state"]
        )

    if adapter_hook := state.get("adapter_hook"):
        adapter = _load_callback(adapter_hook["module"], "adapter hook")
        payload = adapter.on_hook(call("adapter_hook", payload))
        if payload is None:
            return
        if not isinstance(payload, dict):
            raise HookError(
                f"adapter hook {adapter_hook['module']}: on_hook returned "
                f"{type(payload).__name__}, not a payload dict or None"
            )
    word = state["words"].get(label)
    host_hook = state.get("host_hook") if word else None
    host = None if host_hook is None else _load_callback(host_hook["module"], "host hook")
    if word != Word.DONE:
        payload = cut(payload)
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    append(os.path.join(folder, HOOKS), {"ts": stamp, "label": label, "payload": payload})
    if host is not None:
        host.on_hook(word, call("host_hook", payload))


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


def read_state(path: str, *, error: type[Exception] | None = None) -> dict:
    """session.json: a regular file (never followed), at most STATE_MAX bytes, its fields passing
    SESSION_STATE; else `error`.
    """
    error = HookError if error is None else error
    state = _read_state(path, error)
    _validate_state(state, path, error)
    _check_callbacks(state, path, error)
    return state


def _read_state(path, error):
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
    return state


def _validate_state(state, path, error):
    for field, check in SESSION_STATE.items():
        if field not in state or not check(state[field]):
            raise error(f"{path}: bad {field}")


def _check_callbacks(state, path, error):
    for hook in ("adapter_hook", "host_hook"):
        value = state.get(hook, {})
        if hook in state and (
            not isinstance(value, dict)
            or set(value) != {"module", "state", "roots"}
            or not isinstance(value["module"], str)
            or not all(part.isidentifier() for part in value["module"].split("."))
            or not isinstance(value["state"], dict)
            or not isinstance(value["roots"], list)
            or not all(map(_abs, value["roots"]))
            or len(set(value["roots"])) != len(value["roots"])
        ):
            raise error(f"{path}: bad {hook}")


def _check_module(name: str, roots: list[str], kind: str, error):
    paths = sys.path
    parts = name.split(".")
    for index in range(len(parts)):
        # A segment lookup needs no loaded parent, including for namespace packages.
        spec = importlib.machinery.PathFinder.find_spec(parts[index], paths)
        if spec is None or (spec.origin is None and index == len(parts) - 1):
            raise error(f"{kind} {name}: cannot find Python module")
        if spec.origin is not None:
            if not any(
                os.path.commonpath((root, spec.origin)) == os.path.normpath(root) for root in roots
            ):
                raise error(f"{kind} {name}: module outside source roots")
            try:
                info = os.lstat(spec.origin)
            except OSError as failure:
                raise error(f"{kind} {name}: {failure.strerror}") from failure
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()):
                raise error(f"{kind} {name}: not a regular file owned by you or root")
        paths = list(spec.submodule_search_locations or ())


def _load_callback(name: str, kind: str):
    module = importlib.import_module(name)
    if not callable(getattr(module, "on_hook", None)):
        raise HookError(f"{kind} {name}: missing on_hook callback")
    return module


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


def run(argv: list[str], stdin) -> None:
    """One hook call, as `tui_hook.py <dir> <roots JSON> <label>` or `agent-pm-session hook` runs
    it, its payload stdin's bytes (stdin may be None): never raises, never writes stdout; a failure
    keeps its traceback in <dir>/hook.log and one stderr line.
    """
    global HookCall, HookError, Word  # standalone: bound only once the source roots are checked
    types_loaded = False
    try:
        payload = read_payload(stdin.buffer)
        folder, label, state = _prepare(argv)
        from agent_pm.session.engine.types import HookCall, HookError, Word

        types_loaded = True
        _dispatch(folder, label, payload, state)
    except Exception as e:  # noqa: BLE001  # the hook must never fail the client
        if argv and _abs(argv[0]):
            with (
                suppress(OSError),
                open(
                    os.path.join(argv[0], "hook.log"),
                    "a",
                    encoding="utf-8",
                    errors="backslashreplace",
                ) as file,
            ):
                file.write(traceback.format_exc())
        say(
            str(e)
            if not types_loaded
            and isinstance(e, ValueError)
            or types_loaded
            and isinstance(e, HookError)
            else f"{type(e).__name__}: {e}"
        )


if __name__ == "__main__":
    run(sys.argv[1:], sys.stdin)
    sys.exit(0)
