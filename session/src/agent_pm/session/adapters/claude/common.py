"""Claude Code client options, Access grants, launch metadata, usage and session inspection."""

import contextlib
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterator, Mapping
from types import MappingProxyType

from agent_pm.session.adapters import common
from agent_pm.session.adapters.common import UUID, scan
from agent_pm.session.engine.types import (
    COMMAND,
    ConfigError,
    Effort,
    Launch,
    Resumable,
    SessionStatus,
    Spec,
    Usage,
    Window,
)
from agent_pm.utils import (
    LOCAL_ENV_VARS,
    STATE,
    environment_ok,
    json_records,
    number,
    printable_abs,
    resume_command,
    save,
)

TABLE = "[adapters.claude]"
NOT_LITERAL = "*?[]{}()\\,"
STORE = f"{STATE}/sessions/claude"  # under HOME, resolved per call
STATE_KEYS = frozenset({"version", "cwd", "sid", "dirs", "env"})
STATE_MAX = 64 * 1024


def added_dirs(flags: tuple[str, ...]) -> Iterator[str]:
    """Each --add-dir X or --add-dir=X's X before "--"."""
    return (
        flag.value
        for flag in scan(flags, {"--add-dir": "--add-dir"})
        if flag.word == "--add-dir" and flag.value is not None
    )


def _merged(flags: tuple[str, ...], hooks: Mapping) -> tuple[dict, tuple[str, ...]]:
    for flag in scan(flags, {"--settings": "--settings"}):
        if flag.word != "--settings" or flag.value is None:
            continue
        value = flag.value
        try:
            settings = json.loads(value)
        except (ValueError, RecursionError):
            settings = None
        own = settings.setdefault("hooks", {}) if isinstance(settings, dict) else None
        if (
            not isinstance(settings, dict)
            or not isinstance(own, dict)
            or not all(isinstance(own.get(e, []), list) for e in hooks)
        ):
            raise ConfigError(
                f"--settings {value!r:.80}: want a JSON object with a list of hooks per event"
            )
        for event, entries in hooks.items():
            own[event] = [*own.get(event, []), *entries]
        return settings, (*flags[: flag.start], *flags[flag.end :])
    return {"hooks": dict(hooks)}, flags


def _window(value) -> Window | None:
    used = number(value.get("utilization")) if isinstance(value, dict) else None
    return None if used is None else Window(100 * used, number(value.get("resetsAt")))


def _full(window: Window | None, resets: float | None) -> Window:
    """`window` at least 100% used; its reset time, else `resets`."""
    if window is None:
        return Window(100.0, resets)
    return Window(
        max(window.used_percent, 100.0), resets if window.resets_at is None else window.resets_at
    )


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(s, str) for s in value)


def _read_dir(path: str) -> str:
    if not printable_abs(path) or any(c in NOT_LITERAL for c in path):
        raise ConfigError(
            f"claude: read dir {path!r:.80} must be a printable absolute path without {NOT_LITERAL}"
        )
    return path


def _read_rules(reads: tuple[str, ...]) -> tuple[str, ...]:
    dirs = (d for read in reads for d in (_read_dir(read), _read_dir(os.path.realpath(read))))
    return tuple(dict.fromkeys(f"Read(/{d.rstrip('/')}/**)" for d in dirs))


def _state_path(store: str, cwd: str, sid: str) -> str:
    key = hashlib.sha256(f"{os.path.realpath(cwd)}\0{sid}".encode()).hexdigest()
    return os.path.join(store, f"{key}.json")


def _load(store: str, cwd: str, sid: str) -> dict:
    if not os.path.isabs(store):
        raise ConfigError("the store must be an absolute path")
    path = _state_path(store, cwd, sid)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ConfigError(f"no resume state for session {sid}") from None
    with os.fdopen(fd, "rb") as f:
        found = os.fstat(f.fileno())
        if not stat.S_ISREG(found.st_mode):
            raise ConfigError(f"{path}: not a regular file")
        data = f.read(STATE_MAX + 1)
    try:
        state = json.loads(data)
    except (ValueError, RecursionError):
        state = None
    if not (
        isinstance(state, dict)
        and frozenset(state) == STATE_KEYS
        and state["version"] == 1
        and state["sid"] == sid
        and isinstance(state["cwd"], str)
        and os.path.realpath(state["cwd"]) == os.path.realpath(cwd)
        and _strings(state["dirs"])
        and isinstance(state["env"], dict)
        and environment_ok(state["env"])
    ):
        raise ConfigError(f"{path}: bad resume state")
    return state


CHECKS = (
    ("flags", _strings, "a list of strings"),
    ("allow", _strings, "a list of strings"),
    *common.CHECKS,
)


class ClaudeCommon:
    UNSET = (
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_JOB_DIR",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_PID",
        "CLAUDE_EFFORT",
        *sorted(LOCAL_ENV_VARS),
    )
    # claude -p otherwise kills a workflow after 10 idle minutes.
    ENV = MappingProxyType({"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"})
    NOTE = (
        "Pre-approved commands match on their exact text and each shell call starts fresh, so anything else waits "
        "on a permission prompt."
    )
    PROBE = (
        "claude",
        "-p",
        "Reply with OK.",
        "--model",
        "haiku",
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "user",
        "--strict-mcp-config",
    )

    def __init__(self, config: Mapping, *, selector):
        self.selector = selector
        common.check_config(config, TABLE, CHECKS)
        self.flags = tuple(config.get("flags", ()))
        if any(flag.startswith("--setting-sources") for flag in self.flags):
            raise ConfigError(
                f"--setting-sources is set by the Claude adapter: remove it from {TABLE}.flags"
            )
        self.allow = tuple(config.get("allow", ()))
        self.tiers = dict(config.get("tiers", {}))
        self.efforts = dict(config.get("efforts", {}))
        self._env = dict(config.get("env", {}))

    def model(self, tier: int | None) -> str | None:
        if tier is None:
            return None
        if (model := self.tiers.get(str(tier))) is None:
            raise ConfigError(f"no model for tier {tier} in {TABLE}.tiers")
        return model

    def effort(self, effort: Effort | None) -> str | None:
        if effort is None:
            return None
        if (value := self.efforts.get(effort)) is None:
            raise ConfigError(f"no effort for {str(effort)!r} in {TABLE}.efforts")
        return value

    def env(self) -> dict[str, str]:
        return {k: v for k, v in self._env.items() if k not in LOCAL_ENV_VARS}

    def config_dir(self, env: Mapping[str, str] = MappingProxyType({})) -> str:
        """`env`: the run's own, over the config's."""
        return (
            env.get("CLAUDE_CONFIG_DIR")
            or self._env.get("CLAUDE_CONFIG_DIR")
            or os.environ.get("CLAUDE_CONFIG_DIR")
            or os.path.expanduser("~/.claude")
        )

    def transcript(self, cwd: str, sid: str, env: Mapping[str, str] = MappingProxyType({})) -> str:
        project = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
        return os.path.join(self.config_dir(env), "projects", project, f"{sid}.jsonl")

    @property
    def store(self) -> str:
        return os.path.join(os.path.realpath(os.path.expanduser("~")), STORE.removeprefix("~/"))

    def resume(self, cwd: str, sid: str) -> str:
        """A reference to save()'s launch state, which resume_main execs; it never holds env values."""
        return resume_command(cwd, {}, (COMMAND, "client-resume", "claude", self.store, cwd, sid))

    def save(self, spec: Spec) -> None:
        """What resume_main replays: spec's cwd, Access.dirs and config, Access and Spec env."""
        cwd, sid = spec.access.cwd or spec.workdir, self.uuid(spec.sid)
        env = {**self.env(), **spec.access.env, **spec.env}
        state = {
            "version": 1,
            "cwd": cwd,
            "sid": sid,
            "dirs": list(spec.access.dirs),
            "env": {k: v for k, v in env.items() if k not in LOCAL_ENV_VARS},
        }
        try:
            os.makedirs(self.store, 0o700, exist_ok=True)
            os.chmod(self.store, 0o700)
            save(_state_path(self.store, cwd, sid), json.dumps(state), durable=True)
        except OSError as error:
            raise ConfigError(f"claude resume state: {error}") from None

    def resume_main(self, argv) -> int:
        """resume()'s run (STORE CWD SID): execs claude resuming SID with the saved dirs and env
        over this environment, in CWD; 1 and one stderr line when it cannot.
        """
        try:
            store, cwd, sid = argv
        except ValueError:
            print("claude resume: expected STORE CWD SID", file=sys.stderr)
            return 1
        try:
            state = _load(store, cwd, self.uuid(sid))
            added = (arg for d in state["dirs"] for arg in ("--add-dir", d))
            os.chdir(state["cwd"])
            command = ["claude", *self.selector(sid, resume=True), *added]
            os.execvpe(command[0], command, {**os.environ, **state["env"]})
        except (ConfigError, OSError) as exc:
            print(f"claude resume: {exc}", file=sys.stderr)
            return 1
        return 0

    @staticmethod
    def uuid(sid) -> str:
        if isinstance(sid, str) and UUID.fullmatch(sid):
            return sid
        raise ConfigError(f"claude session ids are UUIDs, got {sid!r:.80}")

    def launch(
        self,
        spec: Spec,
        settings: Mapping | None,
        *,
        head: tuple[str, ...] = (),
        output: tuple[str, ...] = (),
        tail: tuple[str, ...] = (),
        env: Mapping[str, str] = MappingProxyType({}),
    ) -> Launch:
        """Build a Claude command with configured settings, access grants and output arguments; env
        under the config's. --mcp-config keeps a project's .mcp.json that a --strict-mcp-config in
        spec.flags would drop.
        """
        if any(flag.word == "--setting-sources" for flag in scan(spec.flags)):
            raise ConfigError("--setting-sources is set by the Claude adapter: use Access.project")
        sid = spec.sid
        model, effort = self.model(spec.tier), self.effort(spec.effort)
        access = spec.access
        cwd = access.cwd or spec.workdir
        mcp = os.path.join(cwd, ".mcp.json")
        allow = (
            *(f"Bash({command})" for command in access.commands),
            *_read_rules(access.reads),
            *self.allow,
        )
        strict = any(flag.arg == "--strict-mcp-config" for flag in scan(spec.flags))
        selection = self.selector(sid, resume=spec.resume)
        if not isinstance(sid, str):
            raise ConfigError(f"session ids are UUIDs, got {sid!r:.80}")
        argv = (
            "claude",
            *head,
            *selection,
            *(("--model", model) if model is not None else ()),
            *(("--effort", effort) if effort is not None else ()),
            *(("--name", spec.name) if spec.name is not None else ()),
            *self.flags,
            "--setting-sources",
            "user,project,local" if access.project else "user",
            *(("--mcp-config", mcp) if access.project and strict and os.path.isfile(mcp) else ()),
            *output,
            *(("--settings", json.dumps(settings)) if settings is not None else ()),
            *(arg for d in access.dirs for arg in ("--add-dir", d)),
            *(("--allowedTools", *allow) if allow else ()),
            *tail,
        )
        merged = {
            k: v
            for k, v in {**env, **self.env(), **access.env, **spec.env}.items()
            if k not in LOCAL_ENV_VARS
        }
        return Launch(
            argv,
            env=merged,
            unset=self.UNSET,
            transcript=self.transcript(cwd, sid, merged),
            resume=self.resume(cwd, sid),
        )

    merged = staticmethod(_merged)

    def probe(self) -> Launch:
        return Launch(self.PROBE, env={**self.ENV, **self.env()}, unset=self.UNSET)

    @staticmethod
    def parse_usage(output: str) -> Usage | None:
        """A successful probe without a rate_limit_event reports no windows."""
        evented, info, succeeded = False, None, False
        for record in json_records(output.splitlines()):
            if record.get("type") == "rate_limit_event":
                evented, info = True, record.get("rate_limit_info")
            elif record.get("type") == "result":
                succeeded = record.get("is_error") is False
        if not evented:
            return Usage(None, None) if succeeded else None
        if not isinstance(info, dict):
            return None
        windows = info.get("unifiedWindows")
        windows = windows if isinstance(windows, dict) else {}
        five_hour = _window(windows.get("five_hour"))
        weeks = filter(
            None,
            (_window(window) for name, window in windows.items() if name.startswith("seven_day")),
        )
        weekly = max(weeks, key=lambda window: window.used_percent, default=None)
        if info.get("status") == "rejected":
            resets = number(info.get("resetsAt"))
            kind = info.get("rateLimitType")
            if isinstance(kind, str) and kind.startswith("seven_day"):
                weekly = _full(weekly, resets)
            else:
                five_hour = _full(five_hour, resets)
        return Usage(five_hour, weekly)

    def session_status(
        self,
        sid: str,
        cwd: str,
        *,
        transcript: str = "",
        env: Mapping[str, str] = MappingProxyType({}),
    ) -> SessionStatus:
        """`env`: the run's access and spec env; its CLAUDE_CONFIG_DIR, if any, holds the transcript."""
        try:
            self.uuid(sid)
        except ConfigError:
            return SessionStatus(Resumable.NO, None, "")
        path = self.transcript(cwd, sid, env)
        root = os.path.realpath(os.path.dirname(path))
        if printable_abs(transcript):
            with contextlib.suppress(OSError):
                recorded = os.path.realpath(transcript)
                if os.path.commonpath((recorded, root)) == root and stat.S_ISREG(
                    os.stat(transcript).st_mode
                ):
                    path = recorded
        try:
            mode = os.stat(path).st_mode
        except (FileNotFoundError, NotADirectoryError):
            resumable = Resumable.NO
        except OSError:
            resumable = Resumable.UNKNOWN
        else:
            resumable = Resumable.YES if stat.S_ISREG(mode) else Resumable.UNKNOWN
        paths = [path]
        folder = os.path.splitext(path)[0]
        if os.path.commonpath((os.path.realpath(folder), root)) == root:
            for top, _, files in os.walk(folder):
                paths += (os.path.join(top, name) for name in files)
        times = []
        for each in paths:
            with contextlib.suppress(OSError):
                times.append(os.lstat(each).st_mtime)
        return SessionStatus(resumable, max(times, default=None), self.resume(cwd, sid))
