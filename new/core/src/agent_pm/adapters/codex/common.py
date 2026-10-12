"""Codex access, settings, usage parsing and inspection; read dirs need no flag (the sandbox reads the whole disk).
Both Modes share ~/.agent-pm/core-new-sessions/codex/; recovery and session files have bounded nonblocking reads with
regular-file, ownership and symlink checks. A plain session's (adapters.base) snapshot saves role_policy=False, its
flags only the model and effort, its roots its writable dirs; a resume's added_dirs count beside the saved roots.
options parses spec.flags up to "--"."""

import json
import math
import os
import re
import shlex
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_pm.adapters.base import EFFORTS, ConfigError, Launch, SessionInfo, Usage, Window

from .sessions import (
    RecoveryError,
    SessionFile,
    SessionState,
    absolute_path,
    bind_session,
    check_writable_roots,
    create_session,
    implicit_writable_roots,
    inside,
    load_session,
    native_id,
    read_metadata,
    read_state,
    recovery_snapshot,
    save_session,
    session_file,
    session_identity,
    session_paths,
    store_directory,
    store_root,
    validate_snapshot,
)

TABLE = "[adapters.codex]"
TIERS = ("1", "2", "3", "4")
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
RESERVED = ("-C", "-p", "resume", "fork", "--last", "--dangerously-bypass-hook-trust")
RESERVED_KEYS = ("hooks", "sandbox_workspace_write")
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


def refuse(flags) -> None:
    for name, value, text in options(flags):
        key = value.partition("=")[0].split(".", 1)[0].strip().strip("\"'") if name == "-c" else ""
        if name in RESERVED or key in RESERVED_KEYS:
            raise ConfigError(f"{text}: the Driver sets Codex's hooks, session and cwd")


def passed(flags) -> list[str]:
    return [word for name, value, _ in options(flags) if name in PASSED for word in (name, value)]


def added_dirs(flags, cwd: str) -> list[str]:
    return [os.path.join(cwd, value) for name, value, _ in options(flags) if name == "--add-dir"]


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
                    and not inside(candidate, workdir)
                    and not inside(real_candidate, real_workdir)
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


def flags(snapshot: dict, *, interactive: bool) -> list[str]:
    model = [
        *(("-m", snapshot["model"]) if snapshot["model"] is not None else ()),
        *(
            ("-c", f"model_reasoning_effort={snapshot['effort']}")
            if snapshot["effort"] is not None
            else ()
        ),
    ]
    if not snapshot["role_policy"]:
        return model
    result = [
        "-s",
        snapshot["sandbox"],
        "-c",
        'approval_policy="on-request"',
        "-c",
        'approvals_reviewer="auto_review"',
    ]
    if snapshot["sandbox"] == "workspace-write":
        result.extend(
            (
                "-c",
                f"sandbox_workspace_write.network_access={str(snapshot['network']).lower()}",
                "-c",
                "sandbox_workspace_write.writable_roots=[]",
            )
        )
    result.extend(model)
    if not snapshot["project"]:
        result.extend(("-c", "project_doc_max_bytes=0"))
    if snapshot["sandbox"] == "workspace-write":
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
    UNSET = (
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CODEX_VERSION",
        "CODEX_SANDBOX",
        "CODEX_SANDBOX_NETWORK_DISABLED",
        "CODEX_PERMISSION_PROFILE",
        "CLAUDECODE",
    )
    DEFAULTS = {
        "NO_COLOR": "1",
        "CODEX_CI": "1",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "GH_PAGER": "cat",
    }

    def __init__(self, config):
        self.config = dict(config)
        sandbox = config.get("sandbox", "danger-full-access")
        if sandbox not in ("danger-full-access", "workspace-write"):
            raise ConfigError("Codex sandbox must be danger-full-access or workspace-write")
        self.config["sandbox"] = sandbox
        if type(config.get("shell_network")) is not bool:
            raise ConfigError("Codex shell_network must be a boolean")
        for name, keys in (("tiers", TIERS), ("efforts", EFFORTS)):
            values = config.get(name, {})
            if not isinstance(values, dict) or any(
                not isinstance(key, str)
                or key not in keys
                or not isinstance(value, str)
                or not value.strip()
                or not value.isprintable()
                for key, value in values.items()
            ):
                raise ConfigError(f"{TABLE}.{name} must map {', '.join(keys)} to printable text")
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
            raise ConfigError(f"{TABLE}.env must map environment names to strings without NUL")

    def _mapped(self, name, value):
        if value is None:
            return None
        result = self.config.get(name, {}).get(str(value))
        if result is None:
            raise ConfigError(f"no mapping for {value!r} in {TABLE}.{name}")
        return result

    @_recover
    def prepare(self, spec, resume: bool, *, persist: bool = True) -> SessionState:
        environment = {**os.environ, **self.DEFAULTS, **self.config.get("env", {})}
        check_writable_roots(store_root(), implicit_writable_roots(environment))
        if resume:
            state = self.load(spec.workdir, spec.sid)
            if state is None:
                raise ConfigError(f"missing Codex recovery state for run ID {spec.sid!r}")
            if state.snapshot["role_policy"] != spec.role_policy:
                raise ConfigError(
                    f"Codex session {spec.sid} was started with "
                    f"role_policy={state.snapshot['role_policy']}"
                )
            if state.native_id is None:
                raise ConfigError(f"missing Codex native ID for run ID {spec.sid!r}")
            snapshot = recovery_snapshot(state)
            if session_file(snapshot, state.native_id) is None:
                raise ConfigError(f"missing Codex session file for native ID {state.native_id}")
            if snapshot["role_policy"] and not snapshot["project"]:
                _guard_untrusted_project(state.workdir, snapshot["cwd"], snapshot["codex_home"])
            return state
        roots = self._new_roots(spec)
        workdir, sid = session_identity(spec.workdir, spec.sid)
        if spec.role_policy:
            _, cwd, home, *dirs = roots
            if not spec.access.project:
                _guard_untrusted_project(workdir, cwd, home)
        else:
            cwd, *dirs, home = roots
        snapshot = validate_snapshot(
            {
                "cwd": cwd,
                "project": spec.access.project,
                "dirs": dirs,
                "commands": list(spec.access.commands),
                "model": self._mapped("tiers", spec.tier),
                "effort": self._mapped("efforts", spec.effort),
                "network": self.config["shell_network"],
                "sandbox": self.config["sandbox"],
                "codex_home": home,
                "role_policy": spec.role_policy,
                "env": dict(spec.access.env),
            }
        )
        return create_session(workdir, sid, snapshot, roots, persist=persist)

    def _new_roots(self, spec) -> list[str]:
        workdir = absolute_path(spec.workdir, "workdir")
        cwd = absolute_path(spec.access.cwd or workdir, "cwd")
        home = absolute_path(
            _config_home(cwd, self.config.get("env", {}).get("CODEX_HOME")), "codex_home"
        )
        if not spec.role_policy:
            added = (absolute_path(path, "dir") for path in added_dirs(spec.flags, cwd))
            return [cwd, *added, home]
        return [workdir, cwd, home, *(absolute_path(path, "dir") for path in spec.access.dirs)]

    @_recover
    def writable_dirs(self, spec, resume: bool) -> tuple[str, ...]:
        if resume:
            state = self.load(spec.workdir, spec.sid)
            if state is None:
                raise ConfigError(f"missing Codex recovery state for run ID {spec.sid!r}")
            roots = state.writable_roots
            if not spec.role_policy:
                added = added_dirs(spec.flags, state.snapshot["cwd"])
                roots = [*roots, *map(os.path.realpath, added)]
        else:
            roots = self._new_roots(spec)
        environment = {**os.environ, **self.DEFAULTS, **self.config.get("env", {})}
        implicit = map(os.path.realpath, implicit_writable_roots(environment))
        return tuple(dict.fromkeys([*roots, *implicit]))

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
        return {
            **({} if interactive else self.DEFAULTS),
            **self.config.get("env", {}),
            "CODEX_HOME": snapshot["codex_home"],
            **snapshot["env"],
        }

    def interactive_unset(self) -> tuple[str, ...]:
        return (
            *self.UNSET,
            *(key for key in self.DEFAULTS if key not in self.config.get("env", {})),
        )

    def probe(self) -> Launch:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_probe.py")
        return Launch(
            (os.path.realpath(sys.executable), script),
            env={**self.DEFAULTS, **self.config.get("env", {})},
            unset=self.UNSET,
        )

    @staticmethod
    def parse_usage(output: str) -> Usage | None:
        """Map native account rate limits to five-hour and weekly usage; malformed data is unknown."""
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
        env = {"CODEX_HOME": snapshot["codex_home"], **snapshot["env"]}
        assign = "".join(f"{name}={shlex.quote(value)} " for name, value in env.items())
        return f"cd {shlex.quote(snapshot['cwd'])} && {assign}{command}"

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
            if state is None or not state.native_id or state.native_id != info.native_id:
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
        except (ConfigError, RecoveryError, OSError):
            return replace(result, resumable="unknown")
