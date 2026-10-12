"""What every Claude Code Mode shares: ClaudeCommon validates [adapters.claude] (an error names the key, never its
value) and gives a tier's model, an effort's value, a command's env and UNSET (a parent session's identity keys, which
a child must not inherit), a session's transcript and resume command, Claude's sid rule (a lowercase UUID: Claude takes
the sid as its own session id), the usage probe and its reading, and a session's SessionStatus.

launch is a Mode's claude command: Claude's session id bound to the sid, the spec's tier and effort, each access dir
(--add-dir) and command (--allowedTools Bash(<command>), then the config's allow), and the project and local setting
sources only for a project run."""

import json
import math
import os
import re
import shlex
import stat
from collections.abc import Mapping
from types import MappingProxyType

from adapters.base import EFFORTS, ConfigError, Launch, SessionStatus, Spec, Usage, Window

TABLE = "[adapters.claude]"
TIERS = ("1", "2", "3", "4")
UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


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


def _table(value, keys=None) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(k, str) and (keys is None or k in keys) and isinstance(v, str)
        for k, v in value.items()
    )


CHECKS = (
    ("flags", _strings, "a list of strings"),
    ("allow", _strings, "a list of strings"),
    ("tiers", lambda v: _table(v, TIERS), 'a table from tier "1"–"4" to a model name'),
    (
        "efforts",
        lambda v: _table(v, EFFORTS),
        f"a table from effort ({', '.join(EFFORTS)}) to a string",
    ),
    ("env", _table, "a table of strings to strings"),
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
    # This core's own plugin (<plugin>@<marketplace>): a session loads the user's plugins, never it.
    PLUGINS = {"enabledPlugins": {"agent-pm@agent-pm": False}}
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

    def __init__(self, config: Mapping):
        for key, ok, what in CHECKS:
            if key in config and not ok(config[key]):
                raise ConfigError(f"{TABLE}.{key} must be {what}")
        self.flags = tuple(config.get("flags", ()))
        if any(flag.startswith("--setting-sources") for flag in self.flags):
            raise ConfigError(f"--setting-sources is the Driver's: remove it from {TABLE}.flags")
        self.allow = tuple(config.get("allow", ()))
        self.tiers = dict(config.get("tiers", {}))
        self.efforts = dict(config.get("efforts", {}))
        self._env = {**self.ENV, **config.get("env", {})}

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

    @staticmethod
    def resume(cwd: str, sid: str, workdir: str | None = None) -> str:
        q = shlex.quote
        command = f"cd {q(cwd)} && claude --resume {q(sid)}"
        if workdir is not None and os.path.realpath(workdir) != os.path.realpath(cwd):
            command += f" --add-dir {q(workdir)}"
        return command

    @staticmethod
    def uuid(sid) -> str:
        if isinstance(sid, str) and UUID.fullmatch(sid):
            return sid
        raise ConfigError(f"claude session ids are UUIDs, got {sid!r:.80}")

    def launch(
        self,
        spec: Spec,
        resume: bool,
        settings: Mapping,
        head: tuple[str, ...] = (),
        output: tuple[str, ...] = (),
        tail: tuple[str, ...] = (),
    ) -> Launch:
        """claude, `head`, the shared args (the module's docstring) with `output` right before --settings, `tail`."""
        sid = self.uuid(spec.sid)
        model, effort = self.model(spec.tier), self.effort(spec.effort)
        access = spec.access
        cwd = access.cwd or spec.workdir
        mcp = os.path.join(cwd, ".mcp.json")
        allow = (*(f"Bash({command})" for command in access.commands), *self.allow)
        argv = (
            "claude",
            *head,
            "--resume" if resume else "--session-id",
            sid,
            *(("--model", model) if model is not None else ()),
            *(("--effort", effort) if effort is not None else ()),
            *self.flags,
            "--setting-sources",
            "user,project,local" if access.project else "user",
            *(("--mcp-config", mcp) if access.project and os.path.isfile(mcp) else ()),
            *output,
            "--settings",
            json.dumps(settings),
            *(arg for d in access.dirs for arg in ("--add-dir", d)),
            *(("--allowedTools", *allow) if allow else ()),
            *tail,
        )
        return Launch(
            argv,
            env=self.env(),
            unset=self.UNSET,
            transcript=self.transcript(cwd, sid),
            resume=self.resume(cwd, sid, spec.workdir),
        )

    def probe(self) -> Launch:
        return Launch(self.PROBE, env=self.env(), unset=self.UNSET)

    @staticmethod
    def parse_usage(output: str) -> Usage | None:
        info = None
        for line in output.splitlines():
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict) and record.get("type") == "rate_limit_event":
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
            try:
                times.append(os.lstat(each).st_mtime)
            except OSError:
                pass
        return SessionStatus(resumable, max(times, default=None), self.resume(cwd, sid))
