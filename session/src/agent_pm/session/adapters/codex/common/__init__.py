"""Codex shared runtime behavior, recovery storage and validated snapshots."""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import datetime
from functools import cached_property, wraps
from types import MappingProxyType
from typing import Literal, overload
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_pm.session.adapters.codex.common import app_server
from agent_pm.session.adapters.codex.common.types import (
    NativeMetadata,
    SessionFile,
    SessionState,
    Snapshot,
)
from agent_pm.session.adapters.common import (
    check_config,
    scan,
    split_flags,
)
from agent_pm.session.engine.types import (
    ConfigError,
    Launch,
    Resumable,
    SessionInfo,
    SessionStatus,
    Spec,
    Usage,
    Window,
)
from agent_pm.utils import number, resume_command


class RecoveryError(Exception):
    pass


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


def added_dirs(argv, cwd: str) -> list[str]:
    return [
        os.path.join(cwd, flag.value)
        for flag in scan(argv, VALUED)
        if flag.word == "--add-dir" and flag.value is not None
    ]


def absolute_path(path: object, label: str) -> str:
    if not isinstance(path, str) or not path or not os.path.isabs(path) or "\x00" in path:
        raise RecoveryError(f"invalid Codex recovery {label}: expected an absolute path")
    return os.path.realpath(path)


def validate_snapshot(value: object) -> Snapshot:
    if not isinstance(value, dict):
        raise RecoveryError("invalid Codex recovery snapshot fields")
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
        raise RecoveryError("invalid Codex recovery snapshot fields")
    env = value["env"]
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and ENV_NAME.fullmatch(k) and isinstance(v, str) and "\0" not in v
        for k, v in env.items()
    ):
        raise RecoveryError("invalid Codex recovery snapshot env")
    flags = value["flags"]
    if not isinstance(flags, list) or any(not isinstance(v, str) or "\0" in v for v in flags):
        raise RecoveryError("invalid Codex recovery snapshot flags")
    paths = {key: absolute_path(value[key], key) for key in ("cwd", "codex_home")}
    if type(value["project"]) is not bool:
        raise RecoveryError("invalid Codex recovery snapshot project")
    for key in ("dirs", "commands"):
        if not isinstance(value[key], list):
            raise RecoveryError(f"invalid Codex recovery snapshot {key}")
    dirs = [absolute_path(item, "dir") for item in value["dirs"]]
    commands = value["commands"]
    if any(not isinstance(item, str) or not item or not item.isprintable() for item in commands):
        raise RecoveryError("invalid Codex recovery snapshot commands")
    for key in ("model", "effort"):
        if value[key] is not None and (
            not isinstance(value[key], str) or not value[key] or not value[key].isprintable()
        ):
            raise RecoveryError(f"invalid Codex recovery snapshot {key}")
    if any(paths[key] != value[key] for key in paths) or dirs != value["dirs"]:
        raise RecoveryError("invalid Codex recovery snapshot path")
    return Snapshot(
        paths["cwd"],
        value["project"],
        tuple(dirs),
        tuple(commands),
        value["model"],
        value["effort"],
        paths["codex_home"],
        tuple(flags),
        env,
    )


def snapshot_data(snapshot: Snapshot) -> dict:
    return {
        "cwd": snapshot.cwd,
        "codex_home": snapshot.codex_home,
        "project": snapshot.project,
        "dirs": list(snapshot.dirs),
        "commands": list(snapshot.commands),
        "model": snapshot.model,
        "effort": snapshot.effort,
        "flags": list(snapshot.flags),
        "env": dict(snapshot.env),
    }


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
        return SessionFile(
            path,
            info.st_mtime,
            NativeMetadata(payload["id"], payload["cwd"], _parent_identifier(payload)),
        )
    finally:
        if fd >= 0:
            os.close(fd)


def _parent_identifier(payload) -> str | None:
    parent, source = payload.get("parent_thread_id"), payload.get("source")
    if parent is None and isinstance(source, dict):
        agent = source.get("subagent")
        spawn = agent.get("thread_spawn") if isinstance(agent, dict) else None
        parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    return parent if native_id(parent) else None


def session_paths(snapshot: Snapshot, identifier: str = "*") -> list[str]:
    root = os.path.join(snapshot.codex_home, "sessions")
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


def session_file(snapshot: Snapshot, identifier: str) -> SessionFile | None:
    if not native_id(identifier):
        raise RecoveryError("invalid Codex recovery native ID")
    matches = session_paths(snapshot, identifier)
    if len(matches) > 1:
        raise RecoveryError(f"duplicate Codex session files for native ID {identifier}")
    if not matches:
        return None
    found = read_metadata(matches[0])
    if found.metadata.id != identifier or found.metadata.cwd != snapshot.cwd:
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


def session_identity(workdir: object, sid: object) -> tuple[str, str]:
    workdir = absolute_path(workdir, "workdir")
    if not isinstance(sid, str) or not sid or len(sid) > 1024 or "\x00" in sid:
        raise RecoveryError("invalid recovery run ID")
    return workdir, sid


def _state(data: object, workdir: str, sid: str) -> SessionState:
    if (
        not isinstance(data, dict)
        or frozenset(data) != _SESSION_KEYS
        or type(data["version"]) is not int
        or data["version"] != _SESSION_VERSION
    ):
        raise RecoveryError("malformed recovery state")
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
    return SessionState(workdir, sid, validate_snapshot(data["snapshot"]), tuple(roots), native)


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


def implicit_writable_roots(environment: Mapping[str, str] | None = None) -> list[str]:
    environment = os.environ if environment is None else environment
    return [
        "/tmp",
        "/private/tmp",
        "/var/tmp",
        tempfile.gettempdir(),
        *([os.path.abspath(environment["TMPDIR"])] if environment.get("TMPDIR") else []),
    ]


def check_writable_roots(root: str, paths: Sequence[str]) -> None:
    actual = os.path.realpath(root)
    for path in paths:
        writable = absolute_path(path, "writable root")
        if os.path.commonpath((actual, writable)) == writable:
            raise RecoveryError(f"recovery state directory {root} is inside writable root {path}")


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
        raise RecoveryError(f"malformed recovery state: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    return data


class _StoreOperation:
    def __init__(self, root: str):
        canonical = absolute_path(root, "store")
        _check_ancestors(root)
        if canonical != root:
            raise RecoveryError("invalid Codex recovery store: expected a canonical path")
        self.root = root

    def directory(self, *, create: bool) -> str | None:
        root = self.root
        parent = os.path.dirname(root)
        base = os.path.dirname(parent)
        check_writable_roots(root, implicit_writable_roots())
        if not _private_dir(base, create=create, config_parent=True):
            return None
        if not _private_dir(parent, create=create):
            return None
        return root if _private_dir(root, create=create) else None

    def _path(self, workdir: str, sid: str) -> str:
        digest = hashlib.sha256((workdir + "\x00" + sid).encode("utf-8")).hexdigest()
        return os.path.join(self.root, digest + ".json")

    def _validate_recovery(self, state: SessionState) -> None:
        snapshot = state.snapshot
        paths = [
            snapshot.cwd,
            snapshot.codex_home,
            *snapshot.dirs,
            *(absolute_path(path, "dir") for path in added_dirs(snapshot.flags, snapshot.cwd)),
        ]
        check_writable_roots(self.root, paths)
        for path in paths:
            if not any(inside(path, grant) for grant in state.writable_roots):
                raise RecoveryError(
                    f"invalid Codex recovery writable grants: no recorded root covers {path}"
                )

    def read(
        self, path: str, workdir: str | None = None, sid: str | None = None
    ) -> SessionState | None:
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
                raise RecoveryError("malformed recovery state")
            workdir, sid = session_identity(data.get("workdir"), data.get("sid"))
        if sid is None:
            raise RecoveryError("invalid recovery state identity")
        state = _state(data, workdir, sid)
        if path != self._path(workdir, sid):
            raise RecoveryError("invalid recovery state identity")
        self._validate_recovery(state)
        check_writable_roots(self.root, state.writable_roots)
        return state

    def load(self, workdir: str, sid: str) -> SessionState | None:
        return self.read(self._path(workdir, sid), workdir, sid)

    @contextmanager
    def locked(self, timeout: float | None = None):
        fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
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

    def write(self, state: SessionState, *, exclusive: bool) -> None:
        payload = json.dumps(
            {
                "version": _SESSION_VERSION,
                "workdir": state.workdir,
                "sid": state.sid,
                "snapshot": snapshot_data(state.snapshot),
                "native_id": state.native_id,
                "writable_roots": list(state.writable_roots),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _MAX_SESSION_BYTES:
            raise RecoveryError("recovery state is too large")
        path = self._path(state.workdir, state.sid)
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=self.root)
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
            directory = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(tmp):
                os.unlink(tmp)

    def validate_for_save(self, state: SessionState) -> None:
        self._validate_recovery(state)
        check_writable_roots(self.root, [*state.writable_roots, *implicit_writable_roots()])


class SessionStore:
    """Recovery mappings and locking; default roots follow HOME, explicit roots pin the store."""

    def __init__(self, root: str | None = None):
        self._root = root

    @property
    def root(self) -> str:
        return (
            self._root
            if self._root is not None
            else os.path.join(
                os.path.realpath(os.path.expanduser("~")), ".agent-pm", "core-new-sessions", "codex"
            )
        )

    @overload
    def directory(self, *, create: Literal[True]) -> str: ...

    @overload
    def directory(self, *, create: bool) -> str | None: ...

    def directory(self, *, create: bool) -> str | None:
        return _StoreOperation(self.root).directory(create=create)

    def create(
        self,
        workdir: str,
        sid: str,
        snapshot: Snapshot,
        writable_roots: Sequence[str],
        *,
        persist: bool = True,
    ) -> SessionState:
        workdir, sid = session_identity(workdir, sid)
        state = SessionState(
            workdir,
            sid,
            snapshot,
            tuple(dict.fromkeys(absolute_path(path, "writable root") for path in writable_roots)),
        )
        if persist:
            return self.save(state)
        _StoreOperation(self.root).validate_for_save(state)
        return state

    def save(self, state: SessionState) -> SessionState:
        operation = _StoreOperation(self.root)
        operation.validate_for_save(state)
        operation.directory(create=True)
        operation.write(state, exclusive=True)
        return state

    def load(self, workdir: str, sid: object) -> SessionState | None:
        operation = _StoreOperation(self.root)
        workdir, sid = session_identity(workdir, sid)
        directory = operation.directory(create=False)
        if directory is None:
            return None
        return operation.load(workdir, sid)

    def bind(
        self,
        workdir: str,
        sid: str,
        identifier: str,
        timeout: float | None = None,
    ) -> SessionState:
        operation = _StoreOperation(self.root)
        workdir, sid = session_identity(workdir, sid)
        if not native_id(identifier):
            raise RecoveryError("invalid Codex recovery native ID")
        directory = operation.directory(create=False)
        if directory is None:
            raise RecoveryError(f"missing recovery state for run ID {sid!r}")
        with operation.locked(timeout):
            existing = operation.load(workdir, sid)
            if existing is None:
                raise RecoveryError(f"missing recovery state for run ID {sid!r}")
            validated = replace(existing, native_id=identifier)
            if existing.native_id is not None:
                if existing.native_id != validated.native_id:
                    raise RecoveryError(
                        f"recovery run ID {sid!r} is already bound to a different native ID"
                    )
                return existing
            operation.write(validated, exclusive=False)
            return validated

    def lookup(self, sid: str, cwd: str) -> SessionState | None:
        operation = _StoreOperation(self.root)
        cwd = absolute_path(cwd, "cwd")
        directory = operation.directory(create=False)
        if directory is None:
            return None
        matches = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.endswith(".json"):
                    state = operation.read(entry.path)
                    if state is not None and state.sid == sid and state.snapshot.cwd == cwd:
                        matches.append(state)
        if len(matches) > 1:
            raise RecoveryError(f"ambiguous Codex binding for run ID {sid!r} in {cwd}")
        return matches[0] if matches else None


TABLE = "[adapters.codex]"
LIMIT_PREFIXES = (
    "You've hit your usage limit.",
    "You've hit your usage limit for ",
    "Quota exceeded. Check your plan and billing details.",
    "Your workspace is out of credits.",
    "You hit your spend cap set in your workspace.",
    "You hit your spend cap set by the owner of your workspace.",
)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
RESET = re.compile(
    r"(?:Try|try) again at (?:(?P<month>[A-Z][a-z]{2}) (?P<day>\d{1,2})"
    r"(?P<suffix>st|nd|rd|th), (?P<year>\d{4}) )?"
    r"(?P<hour>[1-9]|1[0-2]):(?P<minute>[0-5]\d) (?P<period>AM|PM)\.$"
)
RESERVED = ("-C", "-p", "--dangerously-bypass-hook-trust")
RESERVED_KEYS = ("hooks",)
PASSED = ("-c", "--enable", "--disable")


def reset_at(message, environment):
    match = RESET.search(message)
    if match is None:
        return None
    try:
        zone_name = environment.get("TZ", os.environ.get("TZ"))
        zone = ZoneInfo(zone_name.removeprefix(":")) if zone_name else None
        value = match.groupdict()
        today = datetime.now(zone)
        year, month, day = today.year, today.month, today.day
        if value["month"]:
            year, month, day = (
                int(value["year"]),
                MONTHS.index(value["month"]) + 1,
                int(value["day"]),
            )
            suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
            if value["suffix"] != suffix:
                return None
        hour = int(value["hour"]) % 12 + (12 if value["period"] == "PM" else 0)
        local = datetime(year, month, day, hour, int(value["minute"]))
        stamps = set()
        for fold in (0, 1):
            stamp = local.replace(tzinfo=zone, fold=fold).timestamp()
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None, fold=0) == local:
                stamps.add(stamp)
        return stamps.pop() if len(stamps) == 1 else None
    except (ValueError, OverflowError, OSError, ZoneInfoNotFoundError):
        return None


def refuse(flags) -> None:
    for flag in scan(flags, VALUED):
        key = (
            flag.value.partition("=")[0].split(".", 1)[0].strip().strip("\"'")
            if flag.word == "-c" and flag.value is not None
            else ""
        )
        if flag.word in RESERVED or key in RESERVED_KEYS:
            raise ConfigError(f"{flag.text}: the Codex adapter sets hooks, session and cwd")


def passed(flags) -> list[str]:
    return [
        word
        for flag in scan(flags, VALUED)
        if flag.word in PASSED and flag.value is not None
        for word in (flag.word, flag.value)
    ]


def _recover(method):
    @wraps(method)
    def translated(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except RecoveryError as exc:
            raise ConfigError(str(exc)) from exc

    return translated


def _guard_untrusted_project(workdir: str, cwd: str, config_home: str | None = None) -> None:
    real_workdir, real_cwd = os.path.realpath(workdir), os.path.realpath(cwd)
    if not inside(real_cwd, real_workdir):
        raise ConfigError(
            f"untrusted cwd {cwd} is outside the workdir: Codex project settings may load"
        )

    default_home = os.path.abspath(os.path.expanduser("~/.codex"))
    real_default_home = os.path.realpath(default_home)
    config_home = _config_home(cwd, config_home)
    real_config_home = os.path.realpath(config_home)
    if inside(config_home, workdir) or inside(real_config_home, real_workdir):
        raise ConfigError(
            f"CODEX_HOME {config_home} is inside the untrusted workdir {workdir}; move Codex config outside the workdir"
        )

    for start in (cwd, real_cwd):
        ancestor = start
        while True:
            candidate = os.path.join(ancestor, ".codex")
            if os.path.lexists(candidate) and (
                os.path.isdir(candidate) or os.path.islink(candidate)
            ):
                real_candidate = os.path.realpath(candidate)
                genuine_user_home = (
                    candidate in (default_home, real_default_home)
                    and real_candidate == real_default_home
                    and not os.path.islink(candidate)
                    and not inside(candidate, workdir)
                    and not inside(real_candidate, real_workdir)
                )
                if not genuine_user_home:
                    raise ConfigError(
                        f"project Codex settings at {candidate} may load in an untrusted run; use a clean workdir or trusted_dirs"
                    )
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                break
            ancestor = parent


def _config_home(cwd: str, config_home: str | None = None) -> str:
    return os.path.abspath(os.path.join(cwd, os.path.expanduser(config_home or "~/.codex")))


def launch_options(snapshot: Snapshot) -> list[str]:
    result = [
        *(("-m", snapshot.model) if snapshot.model is not None else ()),
        *(
            ("-c", f"model_reasoning_effort={snapshot.effort}")
            if snapshot.effort is not None
            else ()
        ),
    ]
    if not snapshot.project:
        result.extend(("-c", "project_doc_max_bytes=0"))
    for directory in snapshot.dirs:
        result.extend(("--add-dir", directory))
    return result


def launch_flags(snapshot: Snapshot, *, interactive: bool) -> list[str]:
    supplied, _ = split_flags(snapshot.flags)
    result = [*supplied, *launch_options(snapshot)]
    if not interactive:
        result[:0] = ["--json", "--skip-git-repo-check"]
    return result


def _window(window: dict) -> Window | None:
    percent, reset = number(window.get("usedPercent")), number(window.get("resetsAt"))
    if percent is None or percent < 0:
        return None
    return Window(percent, reset if reset is not None and reset >= 0 else None)


def _usage(snapshot) -> Usage | None:
    if not isinstance(snapshot, dict):
        return None
    windows = {}
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, dict):
            continue
        duration = window.get("windowDurationMins")
        if type(duration) is int and duration in (300, 10080) and duration not in windows:
            windows[duration] = window
    return Usage(_window(windows.get(300, {})), _window(windows.get(10080, {})))


class CodexCommon:
    UNSET = (
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CODEX_VERSION",
        "CODEX_SANDBOX",
        "CODEX_SANDBOX_NETWORK_DISABLED",
        "CODEX_PERMISSION_PROFILE",
        "CLAUDECODE",
    )
    DEFAULTS = MappingProxyType(
        {
            "NO_COLOR": "1",
            "CODEX_CI": "1",
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "GH_PAGER": "cat",
        }
    )

    def __init__(self, config, *, selector):
        self.selector = selector
        self.config = dict(config)
        check_config(config, TABLE)

    @cached_property
    def store(self) -> SessionStore:
        return SessionStore()

    def _mapped(self, name, value):
        if value is None:
            return None
        result = self.config.get(name, {}).get(str(value))
        if result is None:
            raise ConfigError(f"no mapping for {value!r} in {TABLE}.{name}")
        return result

    def _explicit_environment(self, spec: Spec) -> dict[str, str]:
        return {**self.config.get("env", {}), **spec.access.env, **spec.env}

    def _environment(
        self, supplied: Mapping[str, str], *, interactive: bool = False, ambient: bool = False
    ) -> dict[str, str]:
        inherited = (
            {key: value for key, value in os.environ.items() if key not in self.UNSET}
            if ambient
            else {}
        )
        result = {**inherited, **({} if interactive else self.DEFAULTS), **supplied}
        if ambient and not result.get("CODEX_HOME"):
            result["CODEX_HOME"] = inherited.get("CODEX_HOME") or "~/.codex"
        return result

    @_recover
    def prepare(self, spec: Spec, *, resume: bool = False, persist: bool = True) -> SessionState:
        if resume:
            state = self.load(spec.workdir, spec.sid)
            if state is None:
                raise ConfigError(f"missing Codex recovery state for run ID {spec.sid!r}")
            if state.native_id is None:
                raise ConfigError(f"missing Codex native ID for run ID {spec.sid!r}")
            snapshot = state.snapshot
            refuse(snapshot.flags)
            environment = self.environment(snapshot, ambient=True)
            check_writable_roots(self.store.root, implicit_writable_roots(environment))
            if session_file(snapshot, state.native_id) is None:
                raise ConfigError(f"missing Codex session file for native ID {state.native_id}")
            if not snapshot.project:
                _guard_untrusted_project(state.workdir, snapshot.cwd, snapshot.codex_home)
            return state
        refuse(spec.flags)
        explicit = self._explicit_environment(spec)
        environment = self._environment(explicit, ambient=True)
        check_writable_roots(self.store.root, implicit_writable_roots(environment))
        roots = self._new_roots(spec, environment)
        workdir, sid = session_identity(spec.workdir, spec.sid)
        cwd, home, *_ = roots
        if not spec.access.project:
            _guard_untrusted_project(workdir, cwd, home)
        snapshot = validate_snapshot(
            {
                "cwd": cwd,
                "project": spec.access.project,
                "dirs": [absolute_path(path, "dir") for path in spec.access.dirs],
                "commands": list(spec.access.commands),
                "model": self._mapped("tiers", spec.tier),
                "effort": self._mapped("efforts", spec.effort and spec.effort.value),
                "codex_home": home,
                "flags": list(spec.flags),
                "env": explicit,
            }
        )
        return self.store.create(workdir, sid, snapshot, roots, persist=persist)

    def _new_roots(self, spec: Spec, environment: Mapping[str, str]) -> list[str]:
        workdir = absolute_path(spec.workdir, "workdir")
        cwd = absolute_path(spec.access.cwd or workdir, "cwd")
        home = absolute_path(
            _config_home(cwd, environment.get("CODEX_HOME")),
            "codex_home",
        )
        added = [absolute_path(path, "dir") for path in added_dirs(spec.flags, cwd)]
        return [
            cwd,
            home,
            *(absolute_path(path, "dir") for path in spec.access.dirs),
            *added,
        ]

    @_recover
    def writable_dirs(self, spec: Spec, *, resume: bool = False) -> tuple[str, ...]:
        if resume:
            state = self.load(spec.workdir, spec.sid)
            if state is None:
                raise ConfigError(f"missing Codex recovery state for run ID {spec.sid!r}")
            roots = state.writable_roots
            environment = self.environment(state.snapshot, ambient=True)
        else:
            environment = self._environment(self._explicit_environment(spec), ambient=True)
            roots = self._new_roots(spec, environment)
        implicit = map(os.path.realpath, implicit_writable_roots(environment))
        return tuple(dict.fromkeys([*roots, *implicit]))

    @_recover
    def save(self, state: SessionState) -> SessionState:
        return self.store.save(state)

    @_recover
    def load(self, workdir: str, sid: object) -> SessionState | None:
        return self.store.load(workdir, sid)

    @_recover
    def bind(self, workdir: str, sid: str, identifier: str) -> SessionState:
        return self.store.bind(workdir, sid, identifier)

    def environment(
        self, snapshot: Snapshot, *, interactive: bool = False, ambient: bool = False
    ) -> dict[str, str]:
        return self._environment(
            {**snapshot.env, "CODEX_HOME": snapshot.codex_home},
            interactive=interactive,
            ambient=ambient,
        )

    def probe(self) -> Launch:
        script = app_server.__file__
        return Launch(
            (os.path.realpath(sys.executable), "-I", script),
            env=self._environment(self.config.get("env", {})),
            unset=self.UNSET,
        )

    @staticmethod
    def parse_usage(output: str) -> Usage | None:
        """Map account limits to five-hour and weekly usage; malformed data is unknown."""
        try:
            result = json.loads(output)
        except (ValueError, TypeError, RecursionError):
            return None
        if not isinstance(result, dict):
            return None
        limits = result.get("rateLimitsByLimitId")
        snapshot = limits.get("codex") if isinstance(limits, dict) else None
        return _usage(snapshot if isinstance(snapshot, dict) else result.get("rateLimits"))

    def resume_command(self, state: SessionState) -> str:
        snapshot = state.snapshot
        selection = self.selector(state.native_id, resume=True)
        _, literal = split_flags(snapshot.flags)
        argv = [
            "codex",
            *selection[:-1],
            *launch_flags(snapshot, interactive=True),
            *selection[-1:],
            *literal,
        ]
        env = {
            "CODEX_HOME": snapshot.codex_home,
            **self.environment(snapshot, interactive=True),
        }
        return resume_command(snapshot.cwd, env, argv)

    @_recover
    def lookup(self, sid: str, cwd: str) -> SessionState | None:
        return self.store.lookup(sid, cwd)

    @_recover
    def last_active(self, state: SessionState, found: SessionFile) -> float:
        candidates = []
        for path in session_paths(state.snapshot):
            try:
                metadata = read_metadata(path)
            except (ConfigError, RecoveryError, OSError):
                continue
            payload = metadata.metadata
            if payload.parent_thread_id is not None:
                candidates.append((payload.id, payload.parent_thread_id, metadata.mtime))
        related, newest = {state.native_id}, found.mtime
        while True:
            children = [
                (identifier, stamp)
                for identifier, parent, stamp in candidates
                if parent in related and identifier not in related
            ]
            added = {identifier for identifier, stamp in children} - related
            if not added:
                return newest
            if len(children) != len(added):
                raise ConfigError("duplicate Codex descendant session files")
            related.update(added)
            newest = max([newest, *(stamp for identifier, stamp in children)])

    def session_metadata(self, info: SessionInfo) -> SessionInfo:
        try:
            state = self.lookup(info.sid, info.cwd)
            if state is None or not state.native_id:
                return info
            found = session_file(state.snapshot, state.native_id)
            return replace(
                info,
                transcript=found.path if found is not None else info.transcript,
                resume=self.resume_command(state),
            )
        except (ConfigError, RecoveryError, OSError):
            return info

    @_recover
    def transcript(self, state: SessionState) -> str:
        found = session_file(state.snapshot, state.native_id) if state.native_id else None
        return found.path if found is not None else ""

    def session_status(self, sid: str, cwd: str) -> SessionStatus:
        resume = ""
        try:
            state = self.lookup(sid, cwd)
            if state is None or state.native_id is None:
                return SessionStatus(Resumable.NO, None, "")
            resume = self.resume_command(state)
            found = session_file(state.snapshot, state.native_id)
            if found is None:
                return SessionStatus(Resumable.NO, None, resume)
            return SessionStatus(Resumable.YES, self.last_active(state, found), resume)
        except (ConfigError, RecoveryError, OSError):
            return SessionStatus(Resumable.UNKNOWN, None, resume)
