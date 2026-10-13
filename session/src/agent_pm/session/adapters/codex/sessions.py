"""Codex recovery records, persistence and snapshot validation; standard library only."""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class CreationOptions:
    root: str | None = None
    persist: bool = True


class RecoveryError(Exception):
    pass


@dataclass(frozen=True)
class SessionFile:
    path: str
    mtime: float
    metadata: dict


@dataclass(frozen=True)
class SessionState:
    workdir: str
    sid: str
    snapshot: dict
    writable_roots: list[str]
    native_id: str | None = None


def private_file(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o600
    )


ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

VALUED = {
    "-c": "-c",
    "--config": "-c",
    "-C": "-C",
    "--cd": "-C",
    "-p": "-p",
    "--profile": "-p",
    "--enable": "--enable",
    "--disable": "--disable",
    "--add-dir": "--add-dir",
}


def split_flags(flags) -> tuple[list[str], list[str]]:
    index = flags.index("--") if "--" in flags else len(flags)
    return list(flags[:index]), list(flags[index:])


def options(flags):
    i = 0
    while i < len(flags) and flags[i] != "--":
        flag = flags[i]
        if flag.startswith("--"):
            word, eq, value = flag.partition("=")
        else:
            word, value = flag[:2], flag[2:].removeprefix("=")
            eq = flag[2:]
        name = VALUED.get(word)
        if name is None:
            yield (word if flag.startswith("--") else flag), None, flag
        elif eq:
            yield name, value, flag
        else:
            i += 1
            value = flags[i] if i < len(flags) else ""
            yield name, value, f"{flag} {value}"
        i += 1


def added_dirs(flags, cwd: str) -> list[str]:
    return [
        os.path.join(cwd, value)
        for name, value, _ in options(flags)
        if name == "--add-dir" and value is not None
    ]


def absolute_path(path: object, label: str) -> str:
    if not isinstance(path, str) or not path or not os.path.isabs(path) or "\x00" in path:
        raise RecoveryError(f"invalid Codex recovery {label}: expected an absolute path")
    return os.path.realpath(path)


def validate_snapshot(value: dict) -> dict:
    if not isinstance(value, dict):
        raise RecoveryError(f"invalid Codex recovery snapshot {'fields'}")
    if set(value) != {
        "cwd",
        "project",
        "dirs",
        "commands",
        "model",
        "effort",
        "codex_home",
        "flags",
        "env",
    }:
        raise RecoveryError(f"invalid Codex recovery snapshot {'fields'}")
    env, flags = _snapshot_inputs(value)
    paths = {key: absolute_path(value[key], key) for key in ("cwd", "codex_home")}
    if type(value["project"]) is not bool:
        raise RecoveryError(f"invalid Codex recovery snapshot {'project'}")
    for key in ("dirs", "commands"):
        if not isinstance(value[key], list):
            raise RecoveryError(f"invalid Codex recovery snapshot {key}")
    dirs = [absolute_path(item, "dir") for item in value["dirs"]]
    commands = value["commands"]
    if any(not isinstance(item, str) or not item or not item.isprintable() for item in commands):
        raise RecoveryError(f"invalid Codex recovery snapshot {'commands'}")
    for key in ("model", "effort"):
        if value[key] is not None and (
            not isinstance(value[key], str) or not value[key] or not value[key].isprintable()
        ):
            raise RecoveryError(f"invalid Codex recovery snapshot {key}")
    validated = {
        **paths,
        "project": value["project"],
        "dirs": dirs,
        "commands": commands.copy(),
        "model": value["model"],
        "effort": value["effort"],
        "flags": list(flags),
        "env": dict(env),
    }
    if validated != value:
        raise RecoveryError(f"invalid Codex recovery snapshot {'path'}")
    return validated


def _snapshot_inputs(value):
    env = value["env"]
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and ENV_NAME.fullmatch(k) and isinstance(v, str) and "\0" not in v
        for k, v in env.items()
    ):
        raise RecoveryError(f"invalid Codex recovery snapshot {'env'}")
    flags = value["flags"]
    if not isinstance(flags, list) or any(not isinstance(v, str) or "\0" in v for v in flags):
        raise RecoveryError(f"invalid Codex recovery snapshot {'flags'}")
    return env, flags


def inside(path: str, root: str) -> bool:
    return os.path.commonpath((path, root)) == root


def read_metadata(path: str) -> SessionFile:
    _check_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RecoveryError(f"cannot read Codex session file {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RecoveryError(f"unsafe Codex session file: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            try:
                metadata = json.loads(stream.readline(128 * 1024))
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise RecoveryError(f"malformed Codex session metadata: {path}") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("type") != "session_meta"
            or not isinstance(metadata.get("payload"), dict)
        ):
            raise RecoveryError(f"malformed Codex session metadata: {path}")
        payload = metadata["payload"]
        if (
            not native_id(payload.get("id"))
            or not path.endswith("-" + payload["id"] + ".jsonl")
            or absolute_path(payload.get("cwd"), "session cwd") != payload["cwd"]
        ):
            raise RecoveryError(f"mismatched Codex session metadata: {path}")
        return SessionFile(path, info.st_mtime, metadata["payload"])
    finally:
        if fd >= 0:
            os.close(fd)


def session_paths(snapshot: dict, identifier: str = "*") -> list[str]:
    root = os.path.join(snapshot["codex_home"], "sessions")
    _check_ancestors(root)
    if not os.path.exists(root):
        return []
    directories = [root]
    for length in (4, 2, 2):
        children = []
        for directory in directories:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if len(entry.name) == length and entry.name.isascii() and entry.name.isdigit():
                        _check_ancestors(entry.path)
                        if entry.is_dir(follow_symlinks=False):
                            children.append(entry.path)
        directories = children
    matches = []
    for directory in directories:
        with os.scandir(directory) as entries:
            matches.extend(
                entry.path
                for entry in entries
                if fnmatch.fnmatchcase(entry.name, f"rollout-*-{identifier}.jsonl")
            )
    return matches


def session_file(snapshot: dict, identifier: str) -> SessionFile | None:
    if not native_id(identifier):
        raise RecoveryError("invalid Codex recovery native ID")
    matches = session_paths(snapshot, identifier)
    if len(matches) > 1:
        raise RecoveryError(f"duplicate Codex session files for native ID {identifier}")
    if not matches:
        return None
    found = read_metadata(matches[0])
    if found.metadata["id"] != identifier or found.metadata["cwd"] != snapshot["cwd"]:
        raise RecoveryError(
            f"mismatched Codex session metadata for native ID {identifier}: {found.path}"
        )
    return found


def native_id(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


_SESSION_VERSION = 1

_SESSION_KEYS = frozenset({"version", "workdir", "sid", "snapshot", "native_id", "writable_roots"})

_MAX_SESSION_BYTES = 64 * 1024


def recovery_snapshot(state: SessionState, *, root: str | None = None) -> dict:
    snapshot = validate_snapshot(state.snapshot)
    paths = [
        snapshot["cwd"],
        snapshot["codex_home"],
        *snapshot["dirs"],
        *(absolute_path(path, "dir") for path in added_dirs(snapshot["flags"], snapshot["cwd"])),
    ]
    check_writable_roots(store_root(root), paths)
    for path in paths:
        if not any(inside(path, grant) for grant in state.writable_roots):
            raise RecoveryError(
                f"invalid Codex recovery writable grants: no recorded root covers {path}"
            )
    return snapshot


def _copy_snapshot(value: object) -> dict:
    try:
        copied = json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise RecoveryError("invalid recovery snapshot: expected JSON-safe state") from exc
    if not isinstance(value, dict) or copied != value:
        raise RecoveryError("invalid recovery snapshot: expected a JSON object")
    return copied


def session_identity(workdir: object, sid: object) -> tuple[str, str]:
    workdir = absolute_path(workdir, "workdir")
    if not isinstance(sid, str) or not sid or len(sid) > 1024 or "\x00" in sid:
        raise RecoveryError("invalid recovery run ID")
    return workdir, sid


def _state(data: object, workdir: str, sid: str) -> SessionState:
    if (
        not isinstance(data, dict)
        or set(data) != _SESSION_KEYS
        or type(data["version"]) is not int
        or data["version"] != _SESSION_VERSION
    ):
        raise RecoveryError(
            "malformed recovery state" if None is None else f"malformed recovery state: {None}"
        )
    if data["workdir"] != workdir or data["sid"] != sid:
        raise RecoveryError("invalid recovery state identity")
    native = data["native_id"]
    if native is not None and not native_id(native):
        raise RecoveryError("invalid recovery native ID")
    roots = data["writable_roots"]
    if not isinstance(roots, list) or any(
        absolute_path(path, "writable root") != path for path in roots
    ):
        raise RecoveryError("invalid recovery writable roots")
    return SessionState(
        workdir, sid, validate_snapshot(_copy_snapshot(data["snapshot"])), roots.copy(), native
    )


def _components(path: str):
    current = os.path.abspath(path)
    parts = []
    while True:
        parts.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            return reversed(parts)
        current = parent


def _check_ancestors(path: str) -> None:
    for component in _components(path):
        if os.path.islink(component):
            raise RecoveryError(f"recovery state path has a symlink: {component}")


def _private_dir(path: str, *, create: bool, config_parent: bool = False) -> bool:
    _check_ancestors(path)
    if not os.path.exists(path):
        if not create:
            return False
        with suppress(FileExistsError):
            os.mkdir(path, 0o700)
        fd = os.open(os.path.dirname(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    _check_ancestors(path)
    info = os.stat(path, follow_symlinks=False)
    mode = stat.S_IMODE(info.st_mode)
    unsafe_mode = mode & 0o022 if config_parent else mode != 0o700
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or unsafe_mode:
        raise RecoveryError(f"unsafe recovery state directory mode or owner: {path}")
    return True


def store_root(root: str | None = None) -> str:
    if root is not None:
        canonical = absolute_path(root, "store")
        _check_ancestors(root)
        if canonical != root:
            raise RecoveryError("invalid Codex recovery store: expected a canonical path")
        return root
    return os.path.join(
        os.path.realpath(os.path.expanduser("~")), ".agent-pm", "core-new-sessions", "codex"
    )


def store_directory(*, create: bool, root: str | None = None) -> str | None:
    root = store_root(root)
    parent = os.path.dirname(root)
    base = os.path.dirname(parent)
    check_writable_roots(root, implicit_writable_roots())
    if not _private_dir(base, create=create, config_parent=True):
        return None
    if not _private_dir(parent, create=create):
        return None
    return root if _private_dir(root, create=create) else None


def _path(root: str, workdir: str, sid: str) -> str:
    digest = hashlib.sha256((workdir + "\x00" + sid).encode("utf-8")).hexdigest()
    return os.path.join(root, digest + ".json")


def implicit_writable_roots(environment: Mapping[str, str] | None = None) -> list[str]:
    environment = os.environ if environment is None else environment
    return [
        "/tmp",
        "/private/tmp",
        "/var/tmp",
        tempfile.gettempdir(),
        *([os.path.abspath(environment["TMPDIR"])] if environment.get("TMPDIR") else []),
    ]


def check_writable_roots(root: str, paths: list[str]) -> None:
    actual = os.path.realpath(root)
    for path in paths:
        writable = absolute_path(path, "writable root")
        if os.path.commonpath((actual, writable)) == writable:
            raise RecoveryError(f"recovery state directory {root} is inside writable root {path}")


def read_state(
    path: str, workdir: str | None = None, sid: str | None = None, *, root: str | None = None
) -> SessionState | None:
    root = store_root(root)
    _check_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecoveryError(f"cannot read recovery state {path}: {exc}") from exc
    data = _state_data(fd, path)
    if workdir is None:
        if not isinstance(data, dict):
            raise RecoveryError(
                "malformed recovery state" if None is None else f"malformed recovery state: {None}"
            )
        workdir, sid = session_identity(data.get("workdir"), data.get("sid"))
    if sid is None:
        raise RecoveryError("invalid recovery state identity")
    state = _state(data, workdir, sid)
    if path != _path(root, workdir, sid):
        raise RecoveryError("invalid recovery state identity")
    recovery_snapshot(state, root=root)
    check_writable_roots(root, state.writable_roots)
    return state


@contextmanager
def _locked(root: str, timeout: float | None = None):
    fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if deadline is not None else 0))
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise RecoveryError("Codex recovery state lock timed out") from None
                time.sleep(0.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write(path: str, state: SessionState, *, exclusive: bool) -> None:
    payload = json.dumps(
        {
            "version": _SESSION_VERSION,
            "workdir": state.workdir,
            "sid": state.sid,
            "snapshot": state.snapshot,
            "native_id": state.native_id,
            "writable_roots": state.writable_roots,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_SESSION_BYTES:
        raise RecoveryError("recovery state is too large")
    root = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=root)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            try:
                os.link(tmp, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise RecoveryError(
                    f"recovery state already exists for run ID {state.sid!r}"
                ) from exc
        else:
            os.replace(tmp, path)
        directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if fd >= 0:
            os.close(fd)
        if os.path.exists(tmp):
            os.unlink(tmp)


def create_session(
    workdir: str,
    sid: str,
    snapshot: dict,
    writable_roots: list[str],
    *,
    options: CreationOptions | None = None,
) -> SessionState:
    options = CreationOptions() if options is None else options
    root = store_root(options.root)
    workdir, sid = session_identity(workdir, sid)
    validate_snapshot(snapshot)
    state = _state(
        {
            "version": _SESSION_VERSION,
            "workdir": workdir,
            "sid": sid,
            "snapshot": snapshot,
            "native_id": None,
            "writable_roots": list(
                dict.fromkeys(absolute_path(path, "writable root") for path in writable_roots)
            ),
        },
        workdir,
        sid,
    )
    recovery_snapshot(state, root=root)
    check_writable_roots(root, [*state.writable_roots, *implicit_writable_roots()])
    return save_session(state, root=root) if options.persist else state


def save_session(state: SessionState, *, root: str | None = None) -> SessionState:
    root = store_root(root)
    recovery_snapshot(state, root=root)
    check_writable_roots(root, [*state.writable_roots, *implicit_writable_roots()])
    root = store_directory(create=True, root=root)
    assert root is not None
    _write(_path(root, state.workdir, state.sid), state, exclusive=True)
    return state


def load_session(workdir: str, sid: object, *, root: str | None = None) -> SessionState | None:
    workdir, sid = session_identity(workdir, sid)
    root = store_directory(create=False, root=root)
    if root is None:
        return None
    state = read_state(_path(root, workdir, sid), workdir, sid, root=root)
    if state is not None:
        check_writable_roots(root, state.writable_roots)
    return state


def bind_session(
    workdir: str,
    sid: str,
    identifier: str,
    timeout: float | None = None,
    *,
    root: str | None = None,
) -> SessionState:
    workdir, sid = session_identity(workdir, sid)
    if not native_id(identifier):
        raise RecoveryError("invalid Codex recovery native ID")
    root = store_directory(create=False, root=root)
    if root is None:
        raise RecoveryError(
            "missing Codex recovery state"
            if sid is None
            else f"missing recovery state for run ID {sid!r}"
        )
    with _locked(root, timeout):
        existing = load_session(workdir, sid, root=root)
        if existing is None:
            raise RecoveryError(
                "missing Codex recovery state"
                if sid is None
                else f"missing recovery state for run ID {sid!r}"
            )
        validated = _state(
            {
                "version": _SESSION_VERSION,
                "workdir": workdir,
                "sid": sid,
                "snapshot": existing.snapshot,
                "native_id": identifier,
                "writable_roots": existing.writable_roots,
            },
            workdir,
            sid,
        )
        if existing.native_id is not None:
            if existing.native_id != validated.native_id:
                raise RecoveryError(
                    f"recovery run ID {sid!r} is already bound to a different native ID"
                )
            return existing
        _write(_path(root, workdir, sid), validated, exclusive=False)
        return validated


def protect(state, dependencies, environment=None, added=()):
    roots = [*state.writable_roots, *added, *implicit_writable_roots(environment)]
    for root in roots:
        root = os.path.realpath(root)
        for dependency in dependencies:
            for path in {os.path.abspath(dependency), os.path.realpath(dependency)}:
                if inside(path, root) or inside(root, path):
                    raise RecoveryError(
                        f"Codex tui writable root {root} could rewrite hook dependency {path}"
                    )


def _state_data(fd: int, path: str):
    try:
        info = os.fstat(fd)
        if not private_file(info):
            raise RecoveryError(f"unsafe recovery state file mode or owner: {path}")
        if info.st_size > _MAX_SESSION_BYTES:
            raise RecoveryError(f"malformed recovery state: file too large: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            text = stream.read(_MAX_SESSION_BYTES + 1)
            if len(text.encode("utf-8")) > _MAX_SESSION_BYTES:
                raise RecoveryError(f"malformed recovery state: file too large: {path}")
            data = json.loads(text)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RecoveryError(
            "malformed recovery state" if path is None else f"malformed recovery state: {path}"
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
    return data
