"""Claude Code runtime options, Access grants, launch metadata, usage and session inspection."""

import contextlib
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from types import MappingProxyType

from agent_pm.session.adapters import common
from agent_pm.session.adapters.flags import scan
from agent_pm.session.adapters.selection import UUID
from agent_pm.session.engine.types import (
    ConfigError,
    Launch,
    SessionStatus,
    Spec,
    Usage,
    Window,
)
from agent_pm.utils import json_records, number, printable_abs, resume_command

TABLE = "[adapters.claude]"
NOT_LITERAL = "*?[]{}()\\,"


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
        common.check(config, TABLE, CHECKS)
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

    def effort(self, effort: str | None) -> str | None:
        if effort is None:
            return None
        if (value := self.efforts.get(effort)) is None:
            raise ConfigError(f"no effort for {effort!r} in {TABLE}.efforts")
        return value

    def env(self) -> dict[str, str]:
        return dict(self._env)

    def config_dir(self) -> str:
        return (
            self._env.get("CLAUDE_CONFIG_DIR")
            or os.environ.get("CLAUDE_CONFIG_DIR")
            or os.path.expanduser("~/.claude")
        )

    def transcript(self, cwd: str, sid: str) -> str:
        project = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
        return os.path.join(self.config_dir(), "projects", project, f"{sid}.jsonl")

    def resume(
        self,
        cwd: str,
        sid: str,
        dirs: tuple[str, ...] = (),
        env: Mapping[str, str] = MappingProxyType({}),
    ) -> str:
        """`dirs`: the granted Access.dirs, each an --add-dir as at launch."""
        added = (arg for d in dirs for arg in ("--add-dir", d))
        return resume_command(cwd, env, ("claude", *self.selector(sid, resume=True), *added))

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
        resume: bool = False,
        head: tuple[str, ...] = (),
        output: tuple[str, ...] = (),
        tail: tuple[str, ...] = (),
        env: Mapping[str, str] = MappingProxyType({}),
    ) -> Launch:
        """Build a Claude command with configured settings, access grants and output arguments; env
        under the config's. --mcp-config keeps a project's .mcp.json that a --strict-mcp-config in
        spec.flags would drop.
        """

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
        selection = self.selector(sid, resume=resume)
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
        return Launch(
            argv,
            env={**env, **self.env(), **access.env, **spec.env},
            unset=self.UNSET,
            transcript=self.transcript(cwd, sid),
            resume=self.resume(cwd, sid, access.dirs, {**access.env, **spec.env}),
        )

    merged = staticmethod(_merged)

    def probe(self) -> Launch:
        return Launch(self.PROBE, env={**self.ENV, **self.env()}, unset=self.UNSET)

    @staticmethod
    def parse_usage(output: str) -> Usage | None:
        info = None
        for record in json_records(output.splitlines()):
            if record.get("type") == "rate_limit_event":
                info = record.get("rate_limit_info")
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

    def session_status(self, sid: str, cwd: str) -> SessionStatus:
        try:
            self.uuid(sid)
        except ConfigError:
            return SessionStatus("no", None, "")
        path = self.transcript(cwd, sid)
        try:
            mode = os.lstat(path).st_mode
        except (FileNotFoundError, NotADirectoryError):
            resumable = "no"
        except OSError:
            resumable = "unknown"
        else:
            resumable = "yes" if stat.S_ISREG(mode) else "unknown"
        paths = [path]
        for top, _, files in os.walk(path[: -len(".jsonl")]):
            paths += (os.path.join(top, name) for name in files)
        times = []
        for each in paths:
            with contextlib.suppress(OSError):
                times.append(os.lstat(each).st_mtime)
        return SessionStatus(resumable, max(times, default=None), self.resume(cwd, sid))
