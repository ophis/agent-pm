"""Codex access, settings, usage parsing and inspection. Both Modes share ~/.agent-pm/core-new-sessions/codex/;
recovery and session files have bounded nonblocking reads with regular-file, ownership and symlink checks."""

import fcntl
import fnmatch
import hashlib
import json
import math
import os
import shlex
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace

from adapters.base import ConfigError, Launch, Usage, Window


def _absolute(path: str, label: str) -> str:
    if not isinstance(path, str) or not path or not os.path.isabs(path) or "\x00" in path:
        raise ConfigError(f"invalid Codex recovery {label}: expected an absolute path")
    return os.path.realpath(path)


def _snapshot(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "cwd",
        "project",
        "dirs",
        "commands",
        "model",
        "effort",
        "network",
        "codex_home",
    }:
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
        if value[key] is not None and (
            not isinstance(value[key], str) or not value[key] or not value[key].isprintable()
        ):
            raise ConfigError(f"invalid Codex recovery snapshot {key}")
    validated = {
        **paths,
        "project": value["project"],
        "dirs": dirs,
        "commands": commands.copy(),
        "model": value["model"],
        "effort": value["effort"],
        "network": value["network"],
    }
    if validated != value:
        raise ConfigError("invalid Codex recovery snapshot path")
    return validated


def _inside(path: str, root: str) -> bool:
    return os.path.commonpath((path, root)) == root


def _guard_untrusted_project(workdir: str, cwd: str, config_home: str | None = None) -> None:
    real_workdir, real_cwd = os.path.realpath(workdir), os.path.realpath(cwd)
    if not _inside(real_cwd, real_workdir):
        raise ConfigError(
            f"untrusted cwd {cwd} is outside the workdir: Codex project settings may load"
        )

    default_home = os.path.abspath(os.path.expanduser("~/.codex"))
    real_default_home = os.path.realpath(default_home)
    config_home = _config_home(cwd, config_home)
    real_config_home = os.path.realpath(config_home)
    if _inside(config_home, workdir) or _inside(real_config_home, real_workdir):
        raise ConfigError(
            f"CODEX_HOME {config_home} is inside the untrusted workdir {workdir}; "
            "move Codex config outside the workdir"
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
                    and not _inside(candidate, workdir)
                    and not _inside(real_candidate, real_workdir)
                )
                if not genuine_user_home:
                    raise ConfigError(
                        f"project Codex settings at {candidate} may load in an untrusted run; "
                        "use a clean workdir or trusted_dirs"
                    )
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                break
            ancestor = parent


def _config_home(cwd: str, config_home: str | None = None) -> str:
    return os.path.abspath(
        os.path.join(
            cwd, os.path.expanduser(config_home or os.environ.get("CODEX_HOME") or "~/.codex")
        )
    )


@dataclass(frozen=True)
class SessionFile:
    path: str
    mtime: float
    metadata: dict


def _metadata(path: str) -> SessionFile:
    _check_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ConfigError(f"cannot read Codex session file {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ConfigError(f"unsafe Codex session file: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            try:
                metadata = json.loads(stream.readline(128 * 1024))
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ConfigError(f"malformed Codex session metadata: {path}") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("type") != "session_meta"
            or not isinstance(metadata.get("payload"), dict)
        ):
            raise ConfigError(f"malformed Codex session metadata: {path}")
        payload = metadata["payload"]
        if (
            not native_id(payload.get("id"))
            or not path.endswith("-" + payload["id"] + ".jsonl")
            or _absolute(payload.get("cwd"), "session cwd") != payload["cwd"]
        ):
            raise ConfigError(f"mismatched Codex session metadata: {path}")
        return SessionFile(path, info.st_mtime, metadata["payload"])
    finally:
        if fd >= 0:
            os.close(fd)


def _session_paths(snapshot: dict, identifier: str = "*") -> list[str]:
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
        raise ConfigError("invalid Codex recovery native ID")
    matches = _session_paths(snapshot, identifier)
    if len(matches) > 1:
        raise ConfigError(f"duplicate Codex session files for native ID {identifier}")
    if not matches:
        return None
    found = _metadata(matches[0])
    if found.metadata["id"] != identifier or found.metadata["cwd"] != snapshot["cwd"]:
        raise ConfigError(
            f"mismatched Codex session metadata for native ID {identifier}: {found.path}"
        )
    return found


def native_id(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def flags(snapshot: dict, *, interactive: bool) -> list[str]:
    result = [
        "-s",
        "workspace-write",
        "-c",
        'approval_policy="on-request"',
        "-c",
        'approvals_reviewer="auto_review"',
        "-c",
        f"sandbox_workspace_write.network_access={str(snapshot['network']).lower()}",
        "-c",
        "sandbox_workspace_write.writable_roots=[]",
    ]
    if snapshot["model"] is not None:
        result.extend(("-m", snapshot["model"]))
    if snapshot["effort"] is not None:
        result.extend(("-c", f"model_reasoning_effort={snapshot['effort']}"))
    if not snapshot["project"]:
        result.extend(("-c", "project_doc_max_bytes=0"))
    for directory in snapshot["dirs"]:
        result.extend(("--add-dir", directory))
    if not interactive:
        result[:0] = ["--json", "--skip-git-repo-check"]
    return result


_SESSION_VERSION = 1
_SESSION_KEYS = frozenset({"version", "workdir", "sid", "snapshot", "native_id", "writable_roots"})
_MAX_SESSION_BYTES = 64 * 1024


@dataclass(frozen=True)
class SessionState:
    workdir: str
    sid: str
    snapshot: dict
    writable_roots: list[str]
    native_id: str | None = None


def _recovery_snapshot(state: SessionState) -> dict:
    snapshot = _snapshot(state.snapshot)
    paths = [state.workdir, snapshot["cwd"], snapshot["codex_home"], *snapshot["dirs"]]
    _unsafe_root(_store_root(), paths)
    for path in paths:
        if not any(_inside(path, grant) for grant in state.writable_roots):
            raise ConfigError(
                f"invalid Codex recovery writable grants: no recorded root covers {path}"
            )
    return snapshot


def _copy_snapshot(value: object) -> dict:
    try:
        copied = json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConfigError("invalid recovery snapshot: expected JSON-safe state") from exc
    if not isinstance(value, dict) or copied != value:
        raise ConfigError("invalid recovery snapshot: expected a JSON object")
    return copied


def _identity(workdir: str, sid: str) -> tuple[str, str]:
    workdir = _absolute(workdir, "workdir")
    if not isinstance(sid, str) or not sid or len(sid) > 1024 or "\x00" in sid:
        raise ConfigError("invalid recovery run ID")
    return workdir, sid


def _state(data: object, workdir: str, sid: str) -> SessionState:
    if (
        not isinstance(data, dict)
        or set(data) != _SESSION_KEYS
        or type(data["version"]) is not int
        or data["version"] != _SESSION_VERSION
    ):
        raise ConfigError("malformed recovery state")
    if data["workdir"] != workdir or data["sid"] != sid:
        raise ConfigError("invalid recovery state identity")
    native = data["native_id"]
    if native is not None and not native_id(native):
        raise ConfigError("invalid recovery native ID")
    roots = data["writable_roots"]
    if not isinstance(roots, list) or any(
        _absolute(path, "writable root") != path for path in roots
    ):
        raise ConfigError("invalid recovery writable roots")
    return SessionState(
        workdir, sid, _snapshot(_copy_snapshot(data["snapshot"])), roots.copy(), native
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
            raise ConfigError(f"recovery state path has a symlink: {component}")


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
        raise ConfigError(f"unsafe recovery state directory mode or owner: {path}")
    return True


def _store_root() -> str:
    return os.path.join(
        os.path.realpath(os.path.expanduser("~")), ".agent-pm", "core-new-sessions", "codex"
    )


def _store(*, create: bool) -> str | None:
    root = _store_root()
    parent = os.path.dirname(root)
    base = os.path.dirname(parent)
    _unsafe_root(root, _implicit_writable_roots())
    if not _private_dir(base, create=create, config_parent=True):
        return None
    if not _private_dir(parent, create=create):
        return None
    return root if _private_dir(root, create=create) else None


def _path(root: str, workdir: str, sid: str) -> str:
    digest = hashlib.sha256((workdir + "\x00" + sid).encode("utf-8")).hexdigest()
    return os.path.join(root, digest + ".json")


def _implicit_writable_roots(environment: dict[str, str] | None = None) -> list[str]:
    environment = os.environ if environment is None else environment
    return [
        "/tmp",
        "/private/tmp",
        "/var/tmp",
        tempfile.gettempdir(),
        *([os.path.abspath(environment["TMPDIR"])] if environment.get("TMPDIR") else []),
    ]


def _unsafe_root(root: str, paths: list[str]) -> None:
    actual = os.path.realpath(root)
    for path in paths:
        writable = _absolute(path, "writable root")
        if os.path.commonpath((actual, writable)) == writable:
            raise ConfigError(f"recovery state directory {root} is inside writable root {path}")


def _read(path: str, workdir: str | None = None, sid: str | None = None) -> SessionState | None:
    _check_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"cannot read recovery state {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ConfigError(f"unsafe recovery state file mode or owner: {path}")
        if info.st_size > _MAX_SESSION_BYTES:
            raise ConfigError(f"malformed recovery state: file too large: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            text = stream.read(_MAX_SESSION_BYTES + 1)
            if len(text.encode("utf-8")) > _MAX_SESSION_BYTES:
                raise ConfigError(f"malformed recovery state: file too large: {path}")
            data = json.loads(text)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ConfigError(f"malformed recovery state: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if workdir is None:
        if not isinstance(data, dict):
            raise ConfigError("malformed recovery state")
        workdir, sid = _identity(data.get("workdir"), data.get("sid"))
        if data.get("workdir") != workdir or path != _path(os.path.dirname(path), workdir, sid):
            raise ConfigError("invalid recovery state identity")
    state = _state(data, workdir, sid)
    _recovery_snapshot(state)
    _unsafe_root(_store_root(), state.writable_roots)
    return state


@contextmanager
def _locked(root: str):
    fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
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
        raise ConfigError("recovery state is too large")
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
                raise ConfigError(
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


def _create_session(
    workdir: str, sid: str, snapshot: dict, writable_roots: list[str]
) -> SessionState:
    workdir, sid = _identity(workdir, sid)
    state = _state(
        {
            "version": _SESSION_VERSION,
            "workdir": workdir,
            "sid": sid,
            "snapshot": snapshot,
            "native_id": None,
            "writable_roots": list(
                dict.fromkeys(
                    [workdir, *(_absolute(path, "writable root") for path in writable_roots)]
                )
            ),
        },
        workdir,
        sid,
    )
    _recovery_snapshot(state)
    _unsafe_root(_store_root(), [*state.writable_roots, *_implicit_writable_roots()])
    root = _store(create=True)
    assert root is not None
    _write(_path(root, workdir, sid), state, exclusive=True)
    return state


def _load_session(workdir: str, sid: str) -> SessionState | None:
    workdir, sid = _identity(workdir, sid)
    root = _store(create=False)
    if root is None:
        return None
    state = _read(_path(root, workdir, sid), workdir, sid)
    if state is not None:
        _unsafe_root(root, [workdir, *state.writable_roots])
    return state


def _bind_session(workdir: str, sid: str, identifier: str) -> SessionState:
    workdir, sid = _identity(workdir, sid)
    if not native_id(identifier):
        raise ConfigError("invalid Codex recovery native ID")
    root = _store(create=False)
    if root is None:
        raise ConfigError(f"missing recovery state for run ID {sid!r}")
    with _locked(root):
        existing = _load_session(workdir, sid)
        if existing is None:
            raise ConfigError(f"missing recovery state for run ID {sid!r}")
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
                raise ConfigError(
                    f"recovery run ID {sid!r} is already bound to a different native ID"
                )
            return existing
        _write(_path(root, workdir, sid), validated, exclusive=False)
        return validated


@dataclass(frozen=True)
class Inspection:
    sid: str
    native_id: str = ""
    cwd: str = ""
    project: bool = False
    transcript: str = ""
    resume: str = ""
    resumable: str = "no"
    last_active: float | None = None


def _number(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _window(window: dict) -> Window | None:
    percent, reset = window.get("usedPercent"), window.get("resetsAt")
    if not _number(percent) or percent < 0:
        return None
    return Window(float(percent), float(reset) if _number(reset) and reset >= 0 else None)


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
    unset = (
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CODEX_VERSION",
        "CODEX_SANDBOX",
        "CODEX_SANDBOX_NETWORK_DISABLED",
        "CODEX_PERMISSION_PROFILE",
        "CLAUDECODE",
    )
    defaults = {
        "NO_COLOR": "1",
        "CODEX_CI": "1",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "GH_PAGER": "cat",
    }

    def __init__(self, config):
        self.config = dict(config)
        if type(config.get("shell_network")) is not bool:
            raise ConfigError("Codex shell_network must be a boolean")
        for name in ("tiers", "efforts"):
            values = config.get(name, {})
            if not isinstance(values, dict) or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not value.strip()
                or not value.isprintable()
                for key, value in values.items()
            ):
                raise ConfigError(f"Codex {name} must map strings to printable text")
        env = config.get("env", {})
        if not isinstance(env, dict) or any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
            for key, value in env.items()
        ):
            raise ConfigError("Codex env must map environment names to strings without NUL")

    def _mapped(self, name, value):
        if value is None:
            return None
        result = self.config.get(name, {}).get(str(value))
        if result is None:
            raise ConfigError(f"no Codex {name} mapping for {value!r}")
        return result

    def prepare(self, spec, resume: bool) -> SessionState:
        environment = {**os.environ, **self.defaults, **self.config.get("env", {})}
        _unsafe_root(_store_root(), _implicit_writable_roots(environment))
        if resume:
            state = self.load(spec.workdir, spec.sid)
            if state is None:
                raise ConfigError(f"missing Codex recovery state for run ID {spec.sid!r}")
            if state.native_id is None:
                raise ConfigError(f"missing Codex native ID for run ID {spec.sid!r}")
            snapshot = _recovery_snapshot(state)
            if session_file(snapshot, state.native_id) is None:
                raise ConfigError(f"missing Codex session file for native ID {state.native_id}")
            if not snapshot["project"]:
                _guard_untrusted_project(state.workdir, snapshot["cwd"], snapshot["codex_home"])
            return state
        roots = self._new_roots(spec)
        workdir, cwd, home, *dirs = roots
        workdir, sid = _identity(workdir, spec.sid)
        if not spec.access.project:
            _guard_untrusted_project(workdir, cwd, home)
        snapshot = _snapshot(
            {
                "cwd": cwd,
                "project": spec.access.project,
                "dirs": dirs,
                "commands": list(spec.access.commands),
                "model": self._mapped("tiers", spec.tier),
                "effort": self._mapped("efforts", spec.effort),
                "network": self.config["shell_network"],
                "codex_home": home,
            }
        )
        return _create_session(workdir, sid, snapshot, roots)

    def _new_roots(self, spec) -> list[str]:
        workdir = _absolute(spec.workdir, "workdir")
        cwd = _absolute(spec.access.cwd or workdir, "cwd")
        home = _absolute(
            _config_home(cwd, self.config.get("env", {}).get("CODEX_HOME")), "codex_home"
        )
        return [workdir, cwd, home, *(_absolute(path, "dir") for path in spec.access.dirs)]

    def writable_dirs(self, spec, resume: bool) -> tuple[str, ...]:
        if resume:
            state = self.load(spec.workdir, spec.sid)
            if state is None:
                raise ConfigError(f"missing Codex recovery state for run ID {spec.sid!r}")
            roots = state.writable_roots
        else:
            roots = self._new_roots(spec)
        environment = {**os.environ, **self.defaults, **self.config.get("env", {})}
        implicit = map(os.path.realpath, _implicit_writable_roots(environment))
        return tuple(dict.fromkeys([*roots, *implicit]))

    def load(self, workdir: str, sid: str) -> SessionState | None:
        return _load_session(workdir, sid)

    def bind(self, workdir: str, sid: str, identifier: str) -> SessionState:
        return _bind_session(workdir, sid, identifier)

    def environment(self, snapshot: dict) -> dict[str, str]:
        return {**self.defaults, **self.config.get("env", {}), "CODEX_HOME": snapshot["codex_home"]}

    def probe(self) -> Launch:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_probe.py")
        return Launch(
            (os.path.realpath(sys.executable), script),
            env={**self.defaults, **self.config.get("env", {})},
            unset=self.unset,
        )

    @staticmethod
    def parse_usage(output: str) -> Usage | None:
        """The account/rateLimits/read result as Usage; None when it is no JSON object or holds no snapshot dict
        (rateLimitsByLimitId.codex, else rateLimits). five_hour and weekly: the first of the snapshot's primary and
        secondary windows lasting 300 and 10080 minutes; None when absent or its usedPercent is no finite number >= 0
        (a resetsAt that is not: None)."""
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
        command = shlex.join(
            ["codex", "resume", *flags(snapshot, interactive=True), state.native_id]
        )
        return f"cd {shlex.quote(snapshot['cwd'])} && CODEX_HOME={shlex.quote(snapshot['codex_home'])} {command}"

    def lookup(self, sid: str, cwd: str) -> SessionState | None:
        cwd = _absolute(cwd, "cwd")
        root = _store(create=False)
        if root is None:
            return None
        matches = []
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.endswith(".json"):
                    state = _read(entry.path)
                    if state is not None and state.sid == sid and state.snapshot["cwd"] == cwd:
                        matches.append(state)
        if len(matches) > 1:
            raise ConfigError(f"ambiguous Codex binding for run ID {sid!r} in {cwd}")
        return matches[0] if matches else None

    def last_active(self, state: SessionState, found: SessionFile) -> float:
        candidates = []
        for path in _session_paths(state.snapshot):
            try:
                metadata = _metadata(path)
            except (ConfigError, OSError):
                continue
            payload = metadata.metadata
            parent = payload.get("parent_thread_id")
            source = payload.get("source")
            if parent is None and isinstance(source, dict):
                agent = source.get("subagent")
                spawn = agent.get("thread_spawn") if isinstance(agent, dict) else None
                parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
            if native_id(parent):
                candidates.append((payload["id"], parent, metadata.mtime))
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

    def inspect(self, sid: str, cwd: str) -> Inspection:
        result = Inspection(sid, cwd=cwd)
        try:
            state = self.lookup(sid, cwd)
            if state is None:
                return result
            result = replace(
                result,
                native_id=state.native_id or "",
                cwd=state.snapshot["cwd"],
                project=state.snapshot["project"],
            )
            if state.native_id is None:
                return result
            result = replace(result, resume=self.resume_command(state))
            found = session_file(state.snapshot, state.native_id)
            if found is None:
                return result
            return replace(
                result,
                transcript=found.path,
                resumable="yes",
                last_active=self.last_active(state, found),
            )
        except (ConfigError, OSError):
            return replace(result, resumable="unknown")
