"""What every Claude Code Mode shares: ClaudeCommon, built from [adapters.claude] (keys: core/config.toml), which it
validates: a wrong type or a --setting-sources flag is a ConfigError naming the key, never its value.

It gives a tier's model and an effort's value; a command's env (ENV, then the config's env) and UNSET, the identity
keys a parent Claude Code session sets, which a child must not inherit; the config dir (that env's CLAUDE_CONFIG_DIR,
else the process's, else ~/.claude) and a session's transcript under it; a human's resume command; uuid(), Claude's sid
rule (a canonical lowercase UUID: Claude takes the sid as its own session id); the usage probe (PROBE) and its stdout as
Usage; a session's SessionStatus from its files. Starts no process."""
import json
import math
import os
import re
import shlex
import stat
from collections.abc import Mapping
from types import MappingProxyType

from adapters.base import EFFORTS, ConfigError, Launch, SessionStatus, Usage

TABLE = "[adapters.claude]"
TIERS = ("1", "2", "3", "4")
UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def number(value) -> float | None:
    """A JSON number (not a bool) as a finite float; None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(s, str) for s in value)


def _table(value, keys=None) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(k, str) and (keys is None or k in keys) and isinstance(v, str) for k, v in value.items())


CHECKS = (("flags", _strings, "a list of strings"),
          ("allow", _strings, "a list of strings"),
          ("tiers", lambda v: _table(v, TIERS), 'a table from tier "1"–"4" to a model name'),
          ("efforts", lambda v: _table(v, EFFORTS), f"a table from effort ({', '.join(EFFORTS)}) to a string"),
          ("env", _table, "a table of strings to strings"))


class ClaudeCommon:
    UNSET = ("CLAUDE_CODE_CHILD_SESSION", "CLAUDE_JOB_DIR", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
             "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH",
             "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_EFFORT")
    # claude -p otherwise kills a workflow after 10 idle minutes.
    ENV = MappingProxyType({"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "3600000"})
    # This core's own plugin (<plugin>@<marketplace>): a session loads the user's plugins, never it.
    PLUGINS = {"enabledPlugins": {"agent-pm@agent-pm": False}}
    NOTE = ("Pre-approved commands match on their exact text and each shell call starts fresh, so anything else waits "
            "on a permission prompt.")
    PROBE = ("claude", "-p", "Reply with OK.", "--model", "haiku", "--output-format", "stream-json", "--verbose",
             "--setting-sources", "user", "--strict-mcp-config")

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
        """The model of `tier`; None: no tier, so no --model."""
        if tier is None:
            return None
        if (model := self.tiers.get(str(tier))) is None:
            raise ConfigError(f"no model for tier {tier} in {TABLE}.tiers")
        return model

    def effort(self, effort: str | None) -> str | None:
        """The --effort value of `effort`; None: no effort, so no --effort."""
        if effort is None:
            return None
        if (value := self.efforts.get(effort)) is None:
            raise ConfigError(f"no effort for {effort!r} in {TABLE}.efforts")
        return value

    def env(self) -> dict[str, str]:
        return dict(self._env)

    def config_dir(self) -> str:
        return self._env.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR") \
            or os.path.expanduser("~/.claude")

    def transcript(self, cwd: str, sid: str) -> str:
        """Where Claude Code saves session `sid` started in `cwd`."""
        project = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
        return os.path.join(self.config_dir(), "projects", project, f"{sid}.jsonl")

    @staticmethod
    def resume(cwd: str, sid: str, workdir: str | None = None) -> str:
        """The shell command a human resumes session `sid` with: claude in its cwd, reaching a `workdir` elsewhere."""
        q = shlex.quote
        command = f"cd {q(cwd)} && claude --resume {q(sid)}"
        if workdir is not None and os.path.realpath(workdir) != os.path.realpath(cwd):
            command += f" --add-dir {q(workdir)}"
        return command

    @staticmethod
    def uuid(sid) -> str:
        """`sid` when it is a canonical lowercase UUID."""
        if isinstance(sid, str) and UUID.fullmatch(sid):
            return sid
        raise ConfigError(f"claude session ids are UUIDs, got {sid!r:.80}")

    def probe(self) -> Launch:
        return Launch(self.PROBE, env=self.env(), unset=self.UNSET)

    @staticmethod
    def parse_usage(output: str) -> Usage | None:
        """The probe's last rate_limit_event as Usage; None when there is none or its five-hour utilization is no
        number. A rejected status folds in: a seven_day* limit type fills the week, any other the five hours."""
        info = None
        for line in output.splitlines():
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict) and record.get("type") == "rate_limit_event":
                info = record.get("rate_limit_info")
        windows = info.get("unifiedWindows") if isinstance(info, dict) else None
        if not isinstance(windows, dict):
            return None
        five = windows.get("five_hour")
        five = number(five.get("utilization")) if isinstance(five, dict) else None
        if five is None:
            return None
        five_hour = 100 * five
        weekly_full = any(name.startswith("seven_day") and isinstance(window, dict)
                          and (used := number(window.get("utilization"))) is not None and used >= 1
                          for name, window in windows.items())
        if info.get("status") == "rejected":
            kind = info.get("rateLimitType")
            if isinstance(kind, str) and kind.startswith("seven_day"):
                weekly_full = True
            else:
                five_hour = max(five_hour, 100.0)
        return Usage(five_hour, weekly_full, number(info.get("resetsAt")))

    def session_status(self, sid: str, cwd: str) -> SessionStatus:
        """Resumable when its transcript is a regular file; last active at the newest write of the transcript and the
        files under its session dir (<transcript without .jsonl>/)."""
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
        for top, _, files in os.walk(path[:-len(".jsonl")]):
            paths += (os.path.join(top, name) for name in files)
        times = []
        for each in paths:
            try:
                times.append(os.lstat(each).st_mtime)
            except OSError:
                pass
        return SessionStatus(resumable, max(times, default=None), self.resume(cwd, sid))
