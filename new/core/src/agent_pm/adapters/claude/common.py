"""Claude Code configuration, launch metadata, usage and session inspection shared by both Modes. A launch's env and
resume command carry a role run's Access.env. plain is a plain session's (adapters.base): the session id, tier, effort
and --name, then --settings, `hooks` merged into the first --settings in spec.flags, then the rest of spec.flags; the
config's env without ENV. refuse: a session flag before "--"."""

import json
import math
import os
import re
import shlex
import stat
from collections.abc import Iterator, Mapping
from types import MappingProxyType

from agent_pm.adapters.base import EFFORTS, ConfigError, Launch, SessionStatus, Spec, Usage, Window
from agent_pm.utils.paths import printable_abs

TABLE = "[adapters.claude]"
TIERS = ("1", "2", "3", "4")
UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
NOT_LITERAL = "*?[]{}()\\,"
REFUSED = (
    "--resume",
    "-r",
    "--session-id",
    "--continue",
    "-c",
    "--fork-session",
    "--from-pr",
    "--teleport",
)


def _before_end(flags: tuple[str, ...]) -> tuple[str, ...]:
    return flags[: flags.index("--")] if "--" in flags else flags


def refuse(flags: tuple[str, ...]) -> None:
    for flag in _before_end(flags):
        word = flag.partition("=")[0] if flag.startswith("--") else flag
        if word in REFUSED:
            raise ConfigError(f"{word}: picks the session; use crew.py start --resume <sid>")


def added_dirs(flags: tuple[str, ...]) -> Iterator[str]:
    """Each --add-dir X or --add-dir=X's X before "--"."""
    i = 0
    while i < len(flags) and flags[i] != "--":
        word, eq, value = flags[i].partition("=")
        if word == "--add-dir":
            if not eq:
                i += 1
                value = flags[i] if i < len(flags) else ""
            yield value
        i += 1


def _merged(flags: tuple[str, ...], hooks: Mapping) -> tuple[dict, tuple[str, ...]]:
    for i, flag in enumerate(_before_end(flags)):
        if flag == "--settings":
            value, end = (flags[i + 1] if i + 1 < len(flags) else ""), i + 2
        elif flag.startswith("--settings="):
            value, end = flag.removeprefix("--settings="), i + 1
        else:
            continue
        try:
            settings = json.loads(value)
        except (ValueError, RecursionError):
            settings = None
        own = settings.setdefault("hooks", {}) if isinstance(settings, dict) else None
        if not isinstance(own, dict) or not all(isinstance(own.get(e, []), list) for e in hooks):
            raise ConfigError(
                f"--settings {value!r:.80}: want a JSON object with a list of hooks per event"
            )
        for event, entries in hooks.items():
            own[event] = [*own.get(event, []), *entries]
        return settings, (*flags[:i], *flags[end:])
    return {"hooks": dict(hooks)}, flags


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


def _read_dir(path: str) -> str:
    if not printable_abs(path) or any(c in NOT_LITERAL for c in path):
        raise ConfigError(
            f"claude: read dir {path!r:.80} must be a printable absolute path without {NOT_LITERAL}"
        )
    return path


def _read_rules(reads: tuple[str, ...]) -> tuple[str, ...]:
    dirs = (d for read in reads for d in (_read_dir(read), _read_dir(os.path.realpath(read))))
    return tuple(dict.fromkeys(f"Read(/{d.rstrip('/')}/**)" for d in dirs))


def _table(value, keys=None) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(k, str) and (keys is None or k in keys) and isinstance(v, str)
        for k, v in value.items()
    )


def _environment(value) -> bool:
    return _table(value) and all(
        key and "=" not in key and "\0" not in key and "\0" not in item
        for key, item in value.items()
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
    ("env", _environment, "a table of environment names to strings without NUL"),
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
        self.config_env = dict(config.get("env", {}))
        self._env = {**self.ENV, **self.config_env}

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
    def resume(
        cwd: str,
        sid: str,
        workdir: str | None = None,
        env: Mapping[str, str] = MappingProxyType({}),
    ) -> str:
        q = shlex.quote
        assign = "".join(f"{name}={q(value)} " for name, value in env.items())
        command = f"cd {q(cwd)} && {assign}claude --resume {q(sid)}"
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
        """Build a Claude command with configured settings, access grants and output arguments."""
        sid = self.uuid(spec.sid)
        model, effort = self.model(spec.tier), self.effort(spec.effort)
        access = spec.access
        cwd = access.cwd or spec.workdir
        mcp = os.path.join(cwd, ".mcp.json")
        allow = (
            *(f"Bash({command})" for command in access.commands),
            *_read_rules(access.reads),
            *self.allow,
        )
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
            env={**self.env(), **access.env},
            unset=self.UNSET,
            transcript=self.transcript(cwd, sid),
            resume=self.resume(cwd, sid, spec.workdir, access.env),
        )

    def plain(self, spec: Spec, resume: bool, hooks: Mapping, tail: tuple[str, ...] = ()) -> Launch:
        sid = self.uuid(spec.sid)
        model, effort = self.model(spec.tier), self.effort(spec.effort)
        settings, flags = _merged(spec.flags, hooks)
        cwd = spec.access.cwd or spec.workdir
        argv = (
            "claude",
            "--resume" if resume else "--session-id",
            sid,
            *(("--model", model) if model is not None else ()),
            *(("--effort", effort) if effort is not None else ()),
            *(("--name", spec.name) if spec.name is not None else ()),
            "--settings",
            json.dumps(settings),
            *flags,
            *tail,
        )
        return Launch(
            argv,
            env=dict(self.config_env),
            unset=self.UNSET,
            transcript=self.transcript(cwd, sid),
            resume=self.resume(cwd, sid),
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
