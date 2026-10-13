from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import runpy
import stat
import tempfile
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_pm.session.adapters.codex.persistence.snapshot import (
        absolute_path,
        added_dirs,
        validate_snapshot,
    )
    from agent_pm.session.adapters.codex.persistence.types import (
        CreationOptions,
        SessionFile,
        SessionState,
    )


_load_file = runpy.run_path(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../hosts/tmux/loading/types.py")
)["load_file"]
_records = _load_file(
    "agent_pm.session.adapters.codex.persistence.types",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "persistence/types.py"),
)
RecoveryError = _records.RecoveryError
private_file = _records.private_file
if not TYPE_CHECKING:
    SessionFile, SessionState = _records.SessionFile, _records.SessionState
    CreationOptions = _records.CreationOptions
    _snapshot = _load_file(
        "agent_pm.session.adapters.codex.persistence.snapshot",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "persistence/snapshot.py"),
    )
    absolute_path = _snapshot.absolute_path
    added_dirs = _snapshot.added_dirs
    validate_snapshot = _snapshot.validate_snapshot


def inside(path: str, root: str) -> bool:
    return os.path.commonpath((path, root)) == root


def read_metadata(path: str) -> SessionFile:
    _check_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise RecoveryError.unreadable_session_file(path, exc) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RecoveryError.unsafe_session_file(path)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            try:
                metadata = json.loads(stream.readline(128 * 1024))
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise RecoveryError.malformed_metadata(path) from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("type") != "session_meta"
            or not isinstance(metadata.get("payload"), dict)
        ):
            raise RecoveryError.malformed_metadata(path)
        payload = metadata["payload"]
        if (
            not native_id(payload.get("id"))
            or not path.endswith("-" + payload["id"] + ".jsonl")
            or absolute_path(payload.get("cwd"), "session cwd") != payload["cwd"]
        ):
            raise RecoveryError.mismatched_metadata(path)
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
        raise RecoveryError.codex_native_id()
    matches = session_paths(snapshot, identifier)
    if len(matches) > 1:
        raise RecoveryError.duplicate_session_files(identifier)
    if not matches:
        return None
    found = read_metadata(matches[0])
    if found.metadata["id"] != identifier or found.metadata["cwd"] != snapshot["cwd"]:
        raise RecoveryError.mismatched_native_metadata(identifier, found.path)
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
            raise RecoveryError.ungranted_path(path)
    return snapshot


def _copy_snapshot(value: object) -> dict:
    try:
        copied = json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise RecoveryError.non_json_state() from exc
    if not isinstance(value, dict) or copied != value:
        raise RecoveryError.non_object_state()
    return copied


def session_identity(workdir: object, sid: object) -> tuple[str, str]:
    workdir = absolute_path(workdir, "workdir")
    if not isinstance(sid, str) or not sid or len(sid) > 1024 or "\x00" in sid:
        raise RecoveryError.run_id()
    return workdir, sid


def _state(data: object, workdir: str, sid: str) -> SessionState:
    if (
        not isinstance(data, dict)
        or set(data) != _SESSION_KEYS
        or type(data["version"]) is not int
        or data["version"] != _SESSION_VERSION
    ):
        raise RecoveryError.malformed_state()
    if data["workdir"] != workdir or data["sid"] != sid:
        raise RecoveryError.identity()
    native = data["native_id"]
    if native is not None and not native_id(native):
        raise RecoveryError.native_id()
    roots = data["writable_roots"]
    if not isinstance(roots, list) or any(
        absolute_path(path, "writable root") != path for path in roots
    ):
        raise RecoveryError.writable_roots()
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
            raise RecoveryError.symlink(component)


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
        raise RecoveryError.unsafe_directory(path)
    return True


def store_root(root: str | None = None) -> str:
    if root is not None:
        canonical = absolute_path(root, "store")
        _check_ancestors(root)
        if canonical != root:
            raise RecoveryError.noncanonical_store()
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
            raise RecoveryError.writable_store(root, path)


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
        raise RecoveryError.unreadable_state(path, exc) from exc
    data = _state_data(fd, path)
    if workdir is None:
        if not isinstance(data, dict):
            raise RecoveryError.malformed_state()
        workdir, sid = session_identity(data.get("workdir"), data.get("sid"))
    if sid is None:
        raise RecoveryError.identity()
    state = _state(data, workdir, sid)
    if path != _path(root, workdir, sid):
        raise RecoveryError.identity()
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
                    raise RecoveryError.lock_timeout() from None
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
        raise RecoveryError.oversized_state()
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
                raise RecoveryError.existing_state(state.sid) from exc
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
        raise RecoveryError.codex_native_id()
    root = store_directory(create=False, root=root)
    if root is None:
        raise RecoveryError.missing_state(sid)
    with _locked(root, timeout):
        existing = load_session(workdir, sid, root=root)
        if existing is None:
            raise RecoveryError.missing_state(sid)
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
                raise RecoveryError.already_bound(sid)
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
                    raise RecoveryError.writable_dependency(root, path)


def _state_data(fd: int, path: str):
    try:
        info = os.fstat(fd)
        if not private_file(info):
            raise RecoveryError.unsafe_state_file(path)
        if info.st_size > _MAX_SESSION_BYTES:
            raise RecoveryError.oversized_state_file(path)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            text = stream.read(_MAX_SESSION_BYTES + 1)
            if len(text.encode("utf-8")) > _MAX_SESSION_BYTES:
                raise RecoveryError.oversized_state_file(path)
            data = json.loads(text)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RecoveryError.malformed_state(path) from exc
    finally:
        if fd >= 0:
            os.close(fd)
    return data
