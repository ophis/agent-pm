"""Codex runtime dialogue, access grants, validated recovery and usage inspection."""

import json
import os
import re
import sys
from dataclasses import replace
from datetime import datetime
from functools import cached_property, wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_pm.session.adapters.codex.sessions import (
    VALUED,
    RecoveryError,
    SessionStore,
    absolute_path,
    added_dirs,
    check_writable_roots,
    implicit_writable_roots,
    inside,
    native_id,
    read_metadata,
    session_file,
    session_identity,
    session_paths,
    validate_snapshot,
)
from agent_pm.session.adapters.codex.types import SessionFile, SessionState
from agent_pm.session.adapters.common import check
from agent_pm.session.adapters.flags import scan, split_flags
from agent_pm.session.engine.types import (
    ConfigError,
    Launch,
    SessionInfo,
    SessionStatus,
    Spec,
    Usage,
    Window,
)
from agent_pm.utils import number, resume_command

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
    supplied, _ = split_flags(snapshot["flags"])
    result = [*supplied, *model]
    if not snapshot["project"]:
        result.extend(("-c", "project_doc_max_bytes=0"))
    for directory in snapshot["dirs"]:
        result.extend(("--add-dir", directory))
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
    DEFAULTS = {
        "NO_COLOR": "1",
        "CODEX_CI": "1",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "GH_PAGER": "cat",
    }

    def __init__(self, config, *, selector):
        self.selector = selector
        self.config = dict(config)
        check(config, TABLE)

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

    @_recover
    def prepare(self, spec: Spec, *, resume: bool = False, persist: bool = True) -> SessionState:
        if resume:
            state = self.load(spec.workdir, spec.sid)
            if state is None:
                raise ConfigError(f"missing Codex recovery state for run ID {spec.sid!r}")
            if state.native_id is None:
                raise ConfigError(f"missing Codex native ID for run ID {spec.sid!r}")
            snapshot = self.store.validate_recovery(state)
            refuse(tuple(snapshot["flags"]))
            check_writable_roots(
                self.store.root, implicit_writable_roots({**os.environ, **snapshot["env"]})
            )
            if session_file(snapshot, state.native_id) is None:
                raise ConfigError(f"missing Codex session file for native ID {state.native_id}")
            if not snapshot["project"]:
                _guard_untrusted_project(state.workdir, snapshot["cwd"], snapshot["codex_home"])
            return state
        refuse(spec.flags)
        environment = {
            **os.environ,
            **self.DEFAULTS,
            **self.config.get("env", {}),
            **spec.access.env,
            **spec.env,
        }
        check_writable_roots(self.store.root, implicit_writable_roots(environment))
        roots = self._new_roots(spec)
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
                "effort": self._mapped("efforts", spec.effort),
                "codex_home": home,
                "flags": list(spec.flags),
                "env": {**self.config.get("env", {}), **spec.access.env, **spec.env},
            }
        )
        return self.store.create(workdir, sid, snapshot, roots, persist=persist)

    def _new_roots(self, spec) -> list[str]:
        workdir = absolute_path(spec.workdir, "workdir")
        cwd = absolute_path(spec.access.cwd or workdir, "cwd")
        home = absolute_path(
            _config_home(
                cwd, {**self.config.get("env", {}), **spec.access.env, **spec.env}.get("CODEX_HOME")
            ),
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
        else:
            roots = self._new_roots(spec)
        environment = {
            **os.environ,
            **self.DEFAULTS,
            **(
                state.snapshot["env"]
                if resume
                else {**self.config.get("env", {}), **spec.access.env, **spec.env}
            ),
        }
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

    def environment(self, snapshot: dict, *, interactive: bool = False) -> dict[str, str]:
        return {
            **({} if interactive else self.DEFAULTS),
            **snapshot["env"],
            "CODEX_HOME": snapshot["codex_home"],
        }

    def probe(self) -> Launch:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_server.py")
        return Launch(
            (os.path.realpath(sys.executable), script),
            env={**self.DEFAULTS, **self.config.get("env", {})},
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
        _, literal = split_flags(snapshot["flags"])
        argv = [
            "codex",
            *selection[:-1],
            *flags(snapshot, interactive=True),
            *selection[-1:],
            *literal,
        ]
        env = {
            "CODEX_HOME": snapshot["codex_home"],
            **{k: v for k, v in snapshot["env"].items() if k != "CODEX_HOME"},
        }
        return resume_command(snapshot["cwd"], env, argv)

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

    def session_status(self, sid: str, cwd: str) -> SessionStatus:
        resume = ""
        try:
            state = self.lookup(sid, cwd)
            if state is None or state.native_id is None:
                return SessionStatus("no", None, "")
            resume = self.resume_command(state)
            found = session_file(state.snapshot, state.native_id)
            if found is None:
                return SessionStatus("no", None, resume)
            return SessionStatus("yes", self.last_active(state, found), resume)
        except (ConfigError, RecoveryError, OSError):
            return SessionStatus("unknown", None, resume)
