"""Codex CLI headless client. Reports are still carried only by report.py."""
import json
import fcntl
import hashlib
import tempfile
import glob
import os
import shlex
import stat
import sys
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from compose import ConfigError, RunConfig, RunParams

from .base import PROGRESS, Access, Client, Event, Launch, SessionInfo


def _absolute(path: str, label: str) -> str:
    if not isinstance(path, str) or not path or not os.path.isabs(path) or "\x00" in path:
        raise ConfigError(f"invalid Codex recovery {label}: expected an absolute path")
    return os.path.realpath(path)


def _snapshot(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"cwd", "project", "dirs", "commands", "model", "effort",
                                                   "network", "codex_home"}:
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
    validated = {**paths, "project": value["project"], "dirs": dirs, "commands": commands.copy(),
                 "model": value["model"], "effort": value["effort"], "network": value["network"]}
    if validated != value:
        raise ConfigError("invalid Codex recovery snapshot path")
    return validated


def _inside(path: str, root: str) -> bool:
    return os.path.commonpath((path, root)) == root


def _guard_untrusted_project(workdir: str, cwd: str, config_home: str | None = None) -> None:
    """Keep project-controlled Codex configuration out of an untrusted run."""
    real_workdir, real_cwd = os.path.realpath(workdir), os.path.realpath(cwd)
    if not _inside(real_cwd, real_workdir):
        raise ConfigError(f"untrusted cwd {cwd} is outside the workdir: Codex project settings may load")

    default_home = os.path.abspath(os.path.expanduser("~/.codex"))
    real_default_home = os.path.realpath(default_home)
    config_home = config_home or os.environ.get("CODEX_HOME") or default_home
    config_home = os.path.abspath(os.path.join(cwd, config_home))
    real_config_home = os.path.realpath(config_home)
    if _inside(config_home, workdir) or _inside(real_config_home, real_workdir):
        raise ConfigError(f"CODEX_HOME {config_home} is inside the untrusted workdir {workdir}; "
                          "move Codex config outside the workdir")

    for start in (cwd, real_cwd):
        ancestor = start
        while True:
            candidate = os.path.join(ancestor, ".codex")
            if os.path.lexists(candidate) and (os.path.isdir(candidate) or os.path.islink(candidate)):
                real_candidate = os.path.realpath(candidate)
                genuine_user_home = (candidate in (default_home, real_default_home)
                                     and os.path.realpath(config_home) == real_default_home
                                     and real_candidate == real_default_home
                                     and not os.path.islink(candidate)
                                     and not _inside(candidate, workdir)
                                     and not _inside(real_candidate, real_workdir))
                if not genuine_user_home:
                    raise ConfigError(f"project Codex settings at {candidate} may load in an untrusted run; "
                                      "use a clean workdir or trusted_dirs")
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                break
            ancestor = parent


def _codex_home(cwd: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.join(
        cwd, os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))))


def _no_symlink_ancestors(path: str) -> None:
    current = os.path.abspath(path)
    while True:
        if os.path.islink(current):
            raise ConfigError(f"unsafe Codex session file symlink: {current}")
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _session_file(snapshot: dict, client_id: str) -> tuple[str, float] | None:
    """Find this exact native session, then verify the CLI's own first metadata event."""
    try:
        if str(uuid.UUID(client_id)) != client_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigError("invalid Codex recovery client ID") from exc
    root = os.path.join(snapshot["codex_home"], "sessions")
    _no_symlink_ancestors(root)
    matches = glob.glob(os.path.join(root, "[0-9]" * 4, "[0-9]" * 2, "[0-9]" * 2,
                                     f"rollout-*-{client_id}.jsonl"))
    if len(matches) > 1:
        raise ConfigError(f"duplicate Codex session files for client ID {client_id}")
    if not matches:
        return None
    path = matches[0]
    _no_symlink_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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
            except (ValueError, UnicodeError) as exc:
                raise ConfigError(f"malformed Codex session metadata: {path}") from exc
        if (not isinstance(metadata, dict) or metadata.get("type") != "session_meta"
                or not isinstance(metadata.get("payload"), dict)
                or metadata["payload"].get("id") != client_id
                or metadata["payload"].get("cwd") != snapshot["cwd"]):
            raise ConfigError(f"mismatched Codex session metadata for client ID {client_id}: {path}")
        return path, info.st_mtime
    finally:
        if fd >= 0:
            os.close(fd)


def _flags(snapshot: dict, *, interactive: bool) -> list[str]:
    flags = ["--approve-for-me", "-m", snapshot["model"],
             "-c", f"model_reasoning_effort={snapshot['effort']}",
             "-c", f"sandbox_workspace_write.network_access={str(snapshot['network']).lower()}"]
    if not snapshot["project"]:
        flags.extend(("-c", "project_doc_max_bytes=0"))
    for directory in snapshot["dirs"]:
        flags.extend(("--add-dir", directory))
    if not interactive:
        flags[:0] = ["--json", "--skip-git-repo-check"]
    return flags


class CodexClient(Client):
    keys = frozenset({"tiers", "efforts", "shell_network"})

    def __init__(self, config: dict):
        super().__init__(config)
        if type(config.get("shell_network")) is not bool:
            raise ConfigError("[clients.codex].shell_network must be a boolean")
        for name, label in (("tiers", "tier"), ("efforts", "effort")):
            values = config.get(name)
            if not isinstance(values, dict):
                raise ConfigError(f"[clients.codex].{name} must be a table")
            for key, value in values.items():
                if not isinstance(value, str) or not value.strip() or not value.isprintable():
                    raise ConfigError(f"[clients.codex] {label} {key!r} must map to printable text")

    def handover(self) -> str:
        return ("Report through `report`, never in a final reply:\n\n"
                f"- **Progress:** at each `[{PROGRESS}:<name>] …` line in your steps, immediately run "
                "`report progress <name> <your report>` before calling the next tool.\n"
                "- **Outcome:** wait for every spawned child using the available wait mechanism and collect each "
                "child result. Finish all background work, then as your last tool action run "
                "`report outcome --status <done|needs_input|failed> --title <one line> --summary <text> "
                "[--question <q>]... [--url <url>] [--file <path>]... [--deliverable <file>]`. "
                "If it fails, fix it and run it again. A final reply is not an outcome.")

    def launch(self, prompt: str, run: RunConfig, *, params: RunParams, access: Access) -> Launch:
        if params.resume:
            state = _load_session(params.workdir, params.sid)
            if state is None or state.client != "codex":
                raise ConfigError(f"missing Codex recovery state for run ID {params.sid!r}")
            if state.client_id is None:
                raise ConfigError(f"missing Codex client ID for run ID {params.sid!r}")
            snapshot = _snapshot(state.snapshot)
            if not _session_file(snapshot, state.client_id):
                raise ConfigError(f"missing Codex session file for client ID {state.client_id}")
            if not snapshot["project"]:
                _guard_untrusted_project(state.workdir, snapshot["cwd"], snapshot["codex_home"])
            argv = ["codex", "exec", *_flags(snapshot, interactive=False),
                    "resume", state.client_id, prompt]
            return Launch(argv, env={"CODEX_HOME": snapshot["codex_home"]}, cwd=snapshot["cwd"],
                          session_state=snapshot, writable_roots=state.writable_roots)
        tiers, efforts = self.config["tiers"], self.config["efforts"]
        model, effort = tiers.get(str(run.tier)), efforts.get(run.effort)
        if model is None:
            raise ConfigError(f"no model for tier {run.tier} in [clients.codex] in config.toml")
        if effort is None:
            raise ConfigError(f"no effort for {run.effort!r} in [clients.codex] in config.toml")
        workdir = os.path.abspath(params.workdir)
        cwd = os.path.abspath(access.cwd or params.workdir)
        if not access.project:
            _guard_untrusted_project(workdir, cwd)
        codex_home = _codex_home(cwd)
        print(f"drive.py: Codex tier {run.tier} uses {model}; "
              "update [clients.codex.tiers] in ~/.agent-pm/core.local.toml when models change",
              file=sys.stderr)
        if access.commands:
            print("drive.py: configured commands are subject to Codex approval; they are not preapproved",
                  file=sys.stderr)
        snapshot = {"cwd": os.path.realpath(cwd), "project": access.project,
                    "dirs": [os.path.realpath(os.path.abspath(directory)) for directory in access.dirs],
                    "commands": access.commands.copy(), "model": model, "effort": effort,
                    "network": self.config["shell_network"], "codex_home": codex_home}
        argv = ["codex", "exec", *_flags({**snapshot, "dirs": access.dirs}, interactive=False)]
        argv.append(prompt)
        return Launch(argv, env={"CODEX_HOME": codex_home}, cwd=cwd, session_state=_snapshot(snapshot),
                      writable_roots=[os.path.realpath(workdir), snapshot["cwd"], codex_home, *snapshot["dirs"]])

    def prepare_session(self, launch: Launch, params: RunParams) -> None:
        workdir = os.path.abspath(params.workdir)
        if params.resume:
            state = _load_session(workdir, params.sid)
            if (state is None or state.client != "codex" or state.snapshot != launch.session_state
                    or state.writable_roots != list(dict.fromkeys([os.path.realpath(workdir),
                                                                  *map(os.path.realpath, launch.writable_roots)]))):
                raise ConfigError(f"missing or changed Codex recovery state for run ID {params.sid!r}")
        else:
            _create_session(workdir, params.sid, launch.session_state, launch.writable_roots)

    def record_session(self, params: RunParams, session_id: str) -> None:
        _bind_session(params.workdir, params.sid, session_id)

    def resume_location(self, workdir: str, sid: str) -> tuple[str, bool]:
        state = _load_session(workdir, sid)
        if state is None or state.client != "codex":
            raise ConfigError(f"missing Codex recovery state for run ID {sid!r}")
        snapshot = _snapshot(state.snapshot)
        return snapshot["cwd"], snapshot["project"]

    def session_info(self, workdir: str, sid: str) -> SessionInfo:
        state = _load_session(workdir, sid)
        if state is None or state.client != "codex":
            return SessionInfo(resume_supported=True, inspection_supported=True)
        snapshot = _snapshot(state.snapshot)
        if state.client_id is None:
            return SessionInfo(resume_supported=True, inspection_supported=True)
        found = _session_file(snapshot, state.client_id)
        if found is None:
            return SessionInfo(resume_supported=True, inspection_supported=True, client_id=state.client_id)
        path, activity = found
        command = ["codex", "resume", *_flags(snapshot, interactive=True), state.client_id]
        manual = f"cd {shlex.quote(snapshot['cwd'])} && CODEX_HOME={shlex.quote(snapshot['codex_home'])} " \
                 + shlex.join(command)
        return SessionInfo(resume_supported=True, inspection_supported=True, client_id=state.client_id,
                           exists=True, transcript=path, last_activity=activity, resume=manual)

    def events(self, lines: Iterable[str]) -> Iterator[Event]:
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                yield Event("stderr", "codex: invalid JSON event")
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "thread.started":
                sid = event.get("thread_id")
                try:
                    if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid:
                        raise ValueError
                except ValueError:
                    yield Event("stderr", "codex: invalid thread.started ID")
                    continue
                yield Event("session", session_id=sid)
            elif kind == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    message = item.get("text")
                    if isinstance(message, str):
                        yield from (Event("text", text) for text in message.splitlines() if text.strip())
            elif kind in ("error", "turn.failed"):
                error = event.get("message") if kind == "error" else event.get("error")
                if isinstance(error, dict):
                    error = error.get("message")
                if isinstance(error, str) and error.strip():
                    yield Event("stderr", error)


_SESSION_VERSION = 2
_SESSION_KEYS = frozenset({"version", "workdir", "sid", "client", "snapshot", "client_id", "writable_roots"})
_MAX_SESSION_BYTES = 64 * 1024


@dataclass(frozen=True)
class _SessionState:
    workdir: str
    sid: str
    client: str
    snapshot: dict
    writable_roots: list[str]
    client_id: str | None = None


def _copy_snapshot(value: object) -> dict:
    """Copy a JSON object without interpreting client-owned fields or coercing types."""
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


def _state(data: object, workdir: str, sid: str) -> _SessionState:
    if not isinstance(data, dict) or set(data) != _SESSION_KEYS or type(data["version"]) is not int \
            or data["version"] != _SESSION_VERSION:
        raise ConfigError("malformed recovery state")
    if data["workdir"] != workdir or data["sid"] != sid:
        raise ConfigError("invalid recovery state identity")
    if not isinstance(data["client"], str) or not data["client"] or not data["client"].isprintable():
        raise ConfigError("invalid recovery client")
    native = data["client_id"]
    if native is not None and (not isinstance(native, str) or not native or len(native) > 1024
                               or not native.isprintable()):
        raise ConfigError("invalid recovery client ID")
    roots = data["writable_roots"]
    if not isinstance(roots, list) or any(_absolute(path, "writable root") != path for path in roots):
        raise ConfigError("invalid recovery writable roots")
    return _SessionState(workdir, sid, data["client"], _copy_snapshot(data["snapshot"]), roots.copy(), native)


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


def _store(*, create: bool) -> str | None:
    base = os.path.join(os.path.realpath(os.path.expanduser("~")), ".agent-pm")
    _unsafe_root(os.path.join(base, "core-sessions"), _implicit_writable_roots())
    if not _private_dir(base, create=create, config_parent=True):
        return None
    root = os.path.join(base, "core-sessions")
    return root if _private_dir(root, create=create) else None


def _path(root: str, workdir: str, sid: str) -> str:
    digest = hashlib.sha256((workdir + "\x00" + sid).encode("utf-8")).hexdigest()
    return os.path.join(root, digest + ".json")


def _implicit_writable_roots() -> list[str]:
    """Temporary locations clients may write without an explicit directory grant."""
    return ["/tmp", "/private/tmp", "/var/tmp", tempfile.gettempdir(),
            *([os.path.abspath(os.environ["TMPDIR"])] if os.environ.get("TMPDIR") else [])]


def _unsafe_root(root: str, paths: list[str]) -> None:
    actual = os.path.realpath(root)
    for path in paths:
        writable = _absolute(path, "writable root")
        if os.path.commonpath((actual, writable)) == writable:
            raise ConfigError(f"recovery state directory {root} is inside writable root {path}")


def _read(path: str, workdir: str, sid: str) -> _SessionState | None:
    _check_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"cannot read recovery state {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ConfigError(f"unsafe recovery state file mode or owner: {path}")
        if info.st_size > _MAX_SESSION_BYTES:
            raise ConfigError(f"malformed recovery state: file too large: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            data = json.load(stream)
    except (ValueError, UnicodeError) as exc:
        raise ConfigError(f"malformed recovery state: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    return _state(data, workdir, sid)


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


def _write(path: str, state: _SessionState, *, exclusive: bool) -> None:
    payload = json.dumps({"version": _SESSION_VERSION, "workdir": state.workdir, "sid": state.sid,
                          "client": state.client, "snapshot": state.snapshot, "client_id": state.client_id,
                          "writable_roots": state.writable_roots},
                         separators=(",", ":")).encode("utf-8")
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
                raise ConfigError(f"recovery state already exists for run ID {state.sid!r}") from exc
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


def _create_session(workdir: str, sid: str, snapshot: dict, writable_roots: list[str]) -> _SessionState:
    """Create a durable launch snapshot before the client process starts."""
    workdir, sid = _identity(workdir, sid)
    state = _state({"version": _SESSION_VERSION, "workdir": workdir, "sid": sid, "client": "codex",
                    "snapshot": snapshot, "client_id": None,
                    "writable_roots": list(dict.fromkeys([workdir, *(_absolute(path, "writable root")
                                                                    for path in writable_roots)]))}, workdir, sid)
    root = os.path.join(os.path.realpath(os.path.expanduser("~")), ".agent-pm", "core-sessions")
    _unsafe_root(root, [*state.writable_roots, *_implicit_writable_roots()])
    root = _store(create=True)
    assert root is not None
    _write(_path(root, workdir, sid), state, exclusive=True)
    return state


def _load_session(workdir: str, sid: str) -> _SessionState | None:
    """Read the authoritative state, or None if no state was ever created."""
    workdir, sid = _identity(workdir, sid)
    root = _store(create=False)
    if root is None:
        return None
    state = _read(_path(root, workdir, sid), workdir, sid)
    if state is not None:
        _unsafe_root(root, [workdir, *state.writable_roots])
    return state


def _bind_session(workdir: str, sid: str, client_id: str) -> _SessionState:
    """Permanently bind the first native ID; the same ID is safe on a resumed turn."""
    workdir, sid = _identity(workdir, sid)
    root = _store(create=False)
    if root is None:
        raise ConfigError(f"missing recovery state for run ID {sid!r}")
    with _locked(root):
        existing = _load_session(workdir, sid)
        if existing is None:
            raise ConfigError(f"missing recovery state for run ID {sid!r}")
        validated = _state({"version": _SESSION_VERSION, "workdir": workdir, "sid": sid,
                            "client": existing.client, "snapshot": existing.snapshot,
                            "client_id": client_id, "writable_roots": existing.writable_roots}, workdir, sid)
        if existing.client_id is not None:
            if existing.client_id != validated.client_id:
                raise ConfigError(f"recovery run ID {sid!r} is already bound to a different client ID")
            return existing
        _write(_path(root, workdir, sid), validated, exclusive=False)
        return validated
