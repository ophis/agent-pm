"""Durable, driver-owned recovery state for native client sessions.

The run's JSONL file is writable by the agent, so it cannot authorize a resume.
Only this private store can supply the native ID and the original access snapshot.
"""
import hashlib
import fcntl
import json
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

from compose import ConfigError


VERSION = 1
SNAPSHOT_KEYS = frozenset({"cwd", "project", "dirs", "commands", "model", "effort", "network", "codex_home"})
STATE_KEYS = frozenset({"version", "workdir", "sid", "client", "snapshot", "client_id"})
MAX_STATE_BYTES = 64 * 1024


@dataclass(frozen=True)
class State:
    workdir: str
    sid: str
    client: str
    snapshot: dict
    client_id: str | None = None


def _absolute(path: str, label: str) -> str:
    if not isinstance(path, str) or not path or not os.path.isabs(path) or "\x00" in path:
        raise ConfigError(f"invalid Codex recovery {label}: expected an absolute path")
    return os.path.realpath(path)


def _snapshot(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != SNAPSHOT_KEYS:
        raise ConfigError("invalid Codex recovery snapshot fields")
    paths = {key: _absolute(value[key], key) for key in ("cwd", "codex_home")}
    for key in ("project", "network"):
        if type(value[key]) is not bool:
            raise ConfigError(f"invalid Codex recovery snapshot {key}")
    for key in ("dirs", "commands"):
        if not isinstance(value[key], list):
            raise ConfigError(f"invalid Codex recovery snapshot {key}")
    dirs = [_absolute(item, "dir") for item in value["dirs"]]
    commands = value["commands"]
    if any(not isinstance(item, str) or not item or not item.isprintable() for item in commands):
        raise ConfigError("invalid Codex recovery snapshot commands")
    for key in ("model", "effort"):
        if not isinstance(value[key], str) or not value[key] or not value[key].isprintable():
            raise ConfigError(f"invalid Codex recovery snapshot {key}")
    return {**paths, "project": value["project"], "dirs": dirs, "commands": commands.copy(),
            "model": value["model"], "effort": value["effort"], "network": value["network"]}


def _identity(workdir: str, sid: str) -> tuple[str, str]:
    workdir = _absolute(workdir, "workdir")
    if not isinstance(sid, str) or not sid or len(sid) > 1024 or "\x00" in sid:
        raise ConfigError("invalid Codex recovery run ID")
    return workdir, sid


def _state(data: object, workdir: str, sid: str) -> State:
    if not isinstance(data, dict) or set(data) != STATE_KEYS or type(data["version"]) is not int \
            or data["version"] != VERSION:
        raise ConfigError("malformed Codex recovery state")
    if data["workdir"] != workdir or data["sid"] != sid:
        raise ConfigError("invalid Codex recovery state identity")
    if not isinstance(data["client"], str) or not data["client"] or not data["client"].isprintable():
        raise ConfigError("invalid Codex recovery client")
    native = data["client_id"]
    if native is not None:
        try:
            if not isinstance(native, str) or str(uuid.UUID(native)) != native:
                raise ValueError
        except ValueError as exc:
            raise ConfigError("invalid Codex recovery client ID") from exc
    return State(workdir, sid, data["client"], _snapshot(data["snapshot"]), native)


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
            raise ConfigError(f"Codex recovery state path has a symlink: {component}")


def _private_dir(path: str, *, create: bool, config_parent: bool = False) -> bool:
    _check_ancestors(path)
    if not os.path.exists(path):
        if not create:
            return False
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
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
        raise ConfigError(f"unsafe Codex recovery state directory mode or owner: {path}")
    return True


def _store(*, create: bool) -> str | None:
    base = os.path.join(os.path.realpath(os.path.expanduser("~")), ".agent-pm")
    if not _private_dir(base, create=create, config_parent=True):
        return None
    root = os.path.join(base, "core-sessions")
    return root if _private_dir(root, create=create) else None


def _path(root: str, workdir: str, sid: str) -> str:
    digest = hashlib.sha256((workdir + "\x00" + sid).encode("utf-8")).hexdigest()
    return os.path.join(root, digest + ".json")


def _unsafe_root(root: str, paths: list[str]) -> None:
    actual = os.path.realpath(root)
    for path in paths:
        writable = _absolute(path, "writable root")
        if os.path.commonpath((actual, writable)) == writable:
            raise ConfigError(f"Codex recovery state directory {root} is inside writable root {path}")


def _read(path: str, workdir: str, sid: str) -> State | None:
    _check_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"cannot read Codex recovery state {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ConfigError(f"unsafe Codex recovery state file mode or owner: {path}")
        if info.st_size > MAX_STATE_BYTES:
            raise ConfigError(f"malformed Codex recovery state: file too large: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            data = json.load(stream)
    except (ValueError, UnicodeError) as exc:
        raise ConfigError(f"malformed Codex recovery state: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    state = _state(data, workdir, sid)
    if data["snapshot"] != state.snapshot:
        raise ConfigError(f"invalid Codex recovery snapshot path: {path}")
    return state


@contextmanager
def _locked(root: str):
    """Serialize native-ID binding across competing resume attempts."""
    fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write(path: str, state: State, *, exclusive: bool) -> None:
    payload = json.dumps({"version": VERSION, "workdir": state.workdir, "sid": state.sid,
                          "client": state.client, "snapshot": state.snapshot, "client_id": state.client_id},
                         separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise ConfigError("Codex recovery state is too large")
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
                raise ConfigError(f"Codex recovery state already exists for run ID {state.sid!r}") from exc
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


def create(workdir: str, sid: str, client: str, snapshot: dict, writable_roots: list[str]) -> State:
    """Create a durable launch snapshot before the client process starts."""
    workdir, sid = _identity(workdir, sid)
    state = _state({"version": VERSION, "workdir": workdir, "sid": sid, "client": client,
                    "snapshot": snapshot, "client_id": None}, workdir, sid)
    root = os.path.join(os.path.realpath(os.path.expanduser("~")), ".agent-pm", "core-sessions")
    _unsafe_root(root, [workdir, state.snapshot["cwd"], state.snapshot["codex_home"],
                        *state.snapshot["dirs"], *writable_roots])
    root = _store(create=True)
    assert root is not None
    _write(_path(root, workdir, sid), state, exclusive=True)
    return state


def load(workdir: str, sid: str) -> State | None:
    """Read the authoritative state, or None if no state was ever created."""
    workdir, sid = _identity(workdir, sid)
    root = _store(create=False)
    if root is None:
        return None
    state = _read(_path(root, workdir, sid), workdir, sid)
    if state is not None:
        _unsafe_root(root, [workdir, state.snapshot["cwd"], state.snapshot["codex_home"],
                            *state.snapshot["dirs"]])
    return state


def bind(workdir: str, sid: str, client_id: str) -> State:
    """Permanently bind the first native ID; the same ID is safe on a resumed turn."""
    workdir, sid = _identity(workdir, sid)
    root = _store(create=False)
    if root is None:
        raise ConfigError(f"missing Codex recovery state for run ID {sid!r}")
    with _locked(root):
        existing = load(workdir, sid)
        if existing is None:
            raise ConfigError(f"missing Codex recovery state for run ID {sid!r}")
        validated = _state({"version": VERSION, "workdir": workdir, "sid": sid,
                            "client": existing.client, "snapshot": existing.snapshot,
                            "client_id": client_id}, workdir, sid)
        if existing.client_id is not None:
            if existing.client_id != validated.client_id:
                raise ConfigError(f"Codex recovery run ID {sid!r} is already bound to a different client ID")
            return existing
        _write(_path(root, workdir, sid), validated, exclusive=False)
        return validated
