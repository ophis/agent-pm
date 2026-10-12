"""Codex access, settings, usage parsing and inspection. Both Modes share ~/.agent-pm/core-new-sessions/codex/;
recovery and session files have bounded nonblocking reads with regular-file, ownership and symlink checks."""
import json
import math
import os
import shlex
import sys
from dataclasses import dataclass, replace
from functools import wraps

from adapters.base import ConfigError, Launch, Usage, Window

from .sessions import (RecoveryError, SessionFile, SessionState, absolute_path, bind_session, create_session,
                       session_identity, implicit_writable_roots, inside, load_session, read_metadata, read_state,
                       recovery_snapshot, save_session, session_paths, validate_snapshot, store_directory, store_root,
                       check_writable_roots, native_id, session_file)


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
        raise ConfigError(f"untrusted cwd {cwd} is outside the workdir: Codex project settings may load")

    default_home = os.path.abspath(os.path.expanduser("~/.codex"))
    real_default_home = os.path.realpath(default_home)
    config_home = _config_home(cwd, config_home)
    real_config_home = os.path.realpath(config_home)
    if inside(config_home, workdir) or inside(real_config_home, real_workdir):
        raise ConfigError(f"CODEX_HOME {config_home} is inside the untrusted workdir {workdir}; "
                          "move Codex config outside the workdir")

    for start in (cwd, real_cwd):
        ancestor = start
        while True:
            candidate = os.path.join(ancestor, ".codex")
            if os.path.lexists(candidate) and (os.path.isdir(candidate) or os.path.islink(candidate)):
                real_candidate = os.path.realpath(candidate)
                genuine_user_home = (candidate in (default_home, real_default_home)
                                     and real_candidate == real_default_home
                                     and not os.path.islink(candidate)
                                     and not inside(candidate, workdir)
                                     and not inside(real_candidate, real_workdir))
                if not genuine_user_home:
                    raise ConfigError(f"project Codex settings at {candidate} may load in an untrusted run; "
                                      "use a clean workdir or trusted_dirs")
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                break
            ancestor = parent


def _config_home(cwd: str, config_home: str | None = None) -> str:
    return os.path.abspath(os.path.join(
        cwd, os.path.expanduser(config_home or os.environ.get("CODEX_HOME") or "~/.codex")))


def flags(snapshot: dict, *, interactive: bool) -> list[str]:
    result = ["-s", "workspace-write", "-c", 'approval_policy="on-request"',
              "-c", 'approvals_reviewer="auto_review"',
              "-c", f"sandbox_workspace_write.network_access={str(snapshot['network']).lower()}",
              "-c", "sandbox_workspace_write.writable_roots=[]"]
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
    unset = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_VERSION", "CODEX_SANDBOX",
             "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_PERMISSION_PROFILE", "CLAUDECODE")
    defaults = {"NO_COLOR": "1", "CODEX_CI": "1", "PAGER": "cat", "GIT_PAGER": "cat", "GH_PAGER": "cat"}

    def __init__(self, config):
        self.config = dict(config)
        if type(config.get("shell_network")) is not bool:
            raise ConfigError("Codex shell_network must be a boolean")
        for name in ("tiers", "efforts"):
            values = config.get(name, {})
            if not isinstance(values, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                                 or not value.strip() or not value.isprintable()
                                                 for key, value in values.items()):
                raise ConfigError(f"Codex {name} must map strings to printable text")
        env = config.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(key, str) or not key or "=" in key or "\0" in key
                                           or not isinstance(value, str) or "\0" in value
                                           for key, value in env.items()):
            raise ConfigError("Codex env must map environment names to strings without NUL")

    def _mapped(self, name, value):
        if value is None:
            return None
        result = self.config.get(name, {}).get(str(value))
        if result is None:
            raise ConfigError(f"no Codex {name} mapping for {value!r}")
        return result

    @_recover
    def prepare(self, spec, resume: bool, *, persist: bool = True) -> SessionState:
        environment = {**os.environ, **self.defaults, **self.config.get("env", {})}
        check_writable_roots(store_root(), implicit_writable_roots(environment))
        if resume:
            state = self.load(spec.workdir, spec.sid)
            if state is None:
                raise ConfigError(f"missing Codex recovery state for run ID {spec.sid!r}")
            if state.native_id is None:
                raise ConfigError(f"missing Codex native ID for run ID {spec.sid!r}")
            snapshot = recovery_snapshot(state)
            if session_file(snapshot, state.native_id) is None:
                raise ConfigError(f"missing Codex session file for native ID {state.native_id}")
            if not snapshot["project"]:
                _guard_untrusted_project(state.workdir, snapshot["cwd"], snapshot["codex_home"])
            return state
        workdir, sid = session_identity(spec.workdir, spec.sid)
        cwd = absolute_path(spec.access.cwd or workdir, "cwd")
        home = absolute_path(_config_home(cwd, self.config.get("env", {}).get("CODEX_HOME")), "codex_home")
        if not spec.access.project:
            _guard_untrusted_project(workdir, cwd, home)
        snapshot = validate_snapshot({"cwd": cwd, "project": spec.access.project,
                              "dirs": [absolute_path(path, "dir") for path in spec.access.dirs],
                              "commands": list(spec.access.commands), "model": self._mapped("tiers", spec.tier),
                              "effort": self._mapped("efforts", spec.effort),
                              "network": self.config["shell_network"], "codex_home": home})
        return create_session(workdir, sid, snapshot, [workdir, cwd, home, *snapshot["dirs"]], persist=persist)

    @_recover
    def save(self, state: SessionState) -> SessionState:
        return save_session(state)

    @_recover
    def load(self, workdir: str, sid: str) -> SessionState | None:
        return load_session(workdir, sid)

    @_recover
    def bind(self, workdir: str, sid: str, identifier: str) -> SessionState:
        return bind_session(workdir, sid, identifier)

    def environment(self, snapshot: dict, *, interactive: bool = False) -> dict[str, str]:
        return {**({} if interactive else self.defaults), **self.config.get("env", {}),
                "CODEX_HOME": snapshot["codex_home"]}

    def interactive_unset(self) -> tuple[str, ...]:
        return (*self.unset, *(key for key in self.defaults if key not in self.config.get("env", {})))

    def probe(self) -> Launch:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_probe.py")
        return Launch((os.path.realpath(sys.executable), script),
                      env={**self.defaults, **self.config.get("env", {})}, unset=self.unset)

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
        command = shlex.join(["codex", "resume", *flags(snapshot, interactive=True), state.native_id])
        return f"cd {shlex.quote(snapshot['cwd'])} && CODEX_HOME={shlex.quote(snapshot['codex_home'])} {command}"

    @_recover
    def lookup(self, sid: str, cwd: str) -> SessionState | None:
        cwd = absolute_path(cwd, "cwd")
        root = store_directory(create=False)
        if root is None:
            return None
        matches = []
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.endswith(".json"):
                    state = read_state(entry.path)
                    if state is not None and state.sid == sid and state.snapshot["cwd"] == cwd:
                        matches.append(state)
        if len(matches) > 1:
            raise ConfigError(f"ambiguous Codex binding for run ID {sid!r} in {cwd}")
        return matches[0] if matches else None

    @_recover
    def last_active(self, state: SessionState, found: SessionFile) -> float:
        candidates = []
        for path in session_paths(state.snapshot):
            try:
                metadata = read_metadata(path)
            except (ConfigError, RecoveryError, OSError):
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
            children = [(identifier, stamp) for identifier, parent, stamp in candidates
                        if parent in related and identifier not in related]
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
            result = replace(result, native_id=state.native_id or "", cwd=state.snapshot["cwd"],
                             project=state.snapshot["project"])
            if state.native_id is None:
                return result
            result = replace(result, resume=self.resume_command(state))
            found = session_file(state.snapshot, state.native_id)
            if found is None:
                return result
            return replace(result, transcript=found.path, resumable="yes", last_active=self.last_active(state, found))
        except (ConfigError, RecoveryError, OSError):
            return replace(result, resumable="unknown")
