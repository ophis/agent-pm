import glob
import json
import os
import re
import shlex
import shutil
from collections.abc import Iterable, Iterator
from types import MappingProxyType

import roster
import tmux_host
from compose import ConfigError, RunConfig, RunParams, report_command, tui_session

from .base import PROGRESS, Access, Client, Event, Launch, SessionInfo, WorkerCommand, WorkerSpec, lines_back


CORE_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APPROVAL = (": pre-approved commands match on their exact text and each shell call starts fresh, "
            "so anything else waits on a permission prompt.")
PROJECTS = os.path.expanduser("~/.claude/projects")
# This core's own plugin (<plugin>@<marketplace>): agent runs load the user's plugins (--setting-sources user), not it.
PLUGINS = {"enabledPlugins": {"agent-pm@agent-pm": False}}
REFUSED = ("--resume", "-r", "--session-id", "--continue", "-c", "--fork-session", "--from-pr", "--teleport")


def resume(cwd: str, sid: str, workdir: str) -> str:
    q = shlex.quote
    cmd = f"cd {q(cwd)} && claude --resume {q(sid)}"
    return cmd if os.path.realpath(cwd) == os.path.realpath(workdir) else f"{cmd} --add-dir {q(workdir)}"


def config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def transcript(cwd: str, sid: str, projects: str = PROJECTS) -> str:
    return os.path.join(projects, re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd)), f"{sid}.jsonl")


class ClaudeClient(Client):
    keys = frozenset({"flags", "tiers", "efforts", "env", "allow", "roles"})
    session_env = "CLAUDECODE"
    command_env = MappingProxyType(dict.fromkeys((
        *tmux_host.PARENT_KEYS, "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PID", "CLAUDE_EFFORT")))

    def session_info(self, workdir: str, sid: str) -> SessionInfo:
        return SessionInfo(resume_supported=True)

    def __init__(self, config: dict):
        super().__init__(config)
        if any(str(f).split("=")[0] == "--setting-sources" for f in config.get("flags", [])):
            raise ConfigError("--setting-sources is the driver's (trusted_dirs): remove it from [clients.claude].flags")

    def approval(self) -> str:
        return APPROVAL

    def handover(self) -> str:
        return ("Report through `report`, never in a reply:\n\n"
                f"- **Progress:** at each `[{PROGRESS}:<name>] …` line in your steps, before calling the next tool, run "
                "`report progress <name> <your report>`, e.g. "
                "`report progress start <what that line asks you to report>`.\n"
                "- **Outcome:** as your last action, after everything else is done and any background work you "
                "started has finished, run `report outcome --status <done|needs_input|failed> --title <one line> "
                "--summary <text> [--question <q>]... [--url <url>] [--file <path>]... [--deliverable <file>]`: "
                "`--question` and `--file` once per item; `--deliverable` a file holding the deliverable, which is "
                "read as its text. If it fails, fix it and run it again.")

    def launch(self, prompt: str, run: RunConfig, *, params: RunParams, access: Access) -> Launch:
        c = self.config
        model, effort = c.get("tiers", {}).get(str(run.tier)), c.get("efforts", {}).get(run.effort)
        if model is None:
            raise ConfigError(f"no model for tier {run.tier} in [clients.claude] in config.toml")
        if effort is None:
            raise ConfigError(f"no effort for {run.effort!r} in [clients.claude] in config.toml")
        head = ["--resume" if params.resume else "--session-id", params.sid, "--model", model, "--effort", effort,
                *c.get("flags", []), "--setting-sources", "user,project,local" if access.project else "user"]
        workdir = os.path.abspath(params.workdir)
        cwd = access.cwd or workdir
        mcp = os.path.join(cwd, ".mcp.json")
        if access.project and os.path.isfile(mcp):
            head += ["--mcp-config", mcp]
        tail = []
        for d in access.dirs:
            tail += ["--add-dir", d]
        allow = [f"Bash({cmd})" for cmd in access.commands] + (self.value(run, "allow") or [])
        if allow:
            tail += ["--allowedTools", *allow]
        argv = ["claude", "-p", prompt, *head, "--output-format", "stream-json", "--verbose", "--settings",
                json.dumps(PLUGINS), *tail]
        stop = report_command(CORE_SCRIPTS, params, self.report_interpreter()) + " stop --pending background_tasks"
        hook = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": stop}]}]}}
        interactive = ["claude", prompt, *head, "--name", tui_session(run.role, params.sid, params.prefix),
                       "--settings", json.dumps({**PLUGINS, **hook}), *tail]
        return Launch(argv, dict(c.get("env", {})), cwd=cwd, interactive=interactive,
                      transcript=transcript(cwd, params.sid), resume=resume(cwd, params.sid, workdir))

    def events(self, lines: Iterable[str]) -> Iterator[Event]:
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                yield Event("text", line.rstrip("\n"))
                continue
            if not isinstance(e, dict) or e.get("type") != "assistant":
                continue
            content = e.get("message", {}).get("content") if isinstance(e.get("message"), dict) else None
            for block in content if isinstance(content, list) else ():
                if isinstance(block, dict) and block.get("type") == "text":
                    yield from (Event("text", t) for t in block.get("text", "").splitlines() if t.strip())

    def worker(self, spec: WorkerSpec) -> WorkerCommand:
        for flag in spec.flags:
            if flag == "--":
                break
            word = flag.split("=", 1)[0] if flag.startswith("--") else flag
            if word in REFUSED:
                raise ConfigError(f"{word}: crew.py picks the session; use start --resume <sid>")
        exe = spec.exe
        if exe is None:
            exe = shutil.which("claude", path=spec.env.get("PATH"))
            if exe is None:
                raise ConfigError("claude not found on PATH")
            exe = os.path.abspath(exe)
        argv = [exe, "--resume" if spec.resume else "--session-id", spec.sid, "--name", spec.name, *spec.flags,
                *(["--", spec.prompt] if spec.prompt else [])]
        try:
            return WorkerCommand(tmux_host.with_hooks(argv, spec.events))
        except tmux_host.TuiError as e:
            raise ConfigError(str(e)) from e

    def worker_reply(self, sid: str) -> str:
        if not roster.SID.fullmatch(sid):
            raise ConfigError(f"bad session id {sid!r}")
        found = glob.glob(os.path.join(glob.escape(config_dir()), "projects", "*", sid + ".jsonl"))
        if not found:
            raise ConfigError(f"no transcript for {sid}: started with a leaked Claude Code variable?")
        last, texts = None, []
        try:
            with open(found[0], "rb") as f:
                for raw in lines_back(f):
                    try:
                        entry = json.loads(raw.decode("utf-8", errors="replace"))
                        message = entry["message"]
                        content = message["content"]
                        if entry["type"] != "assistant" or not isinstance(content, list):
                            continue
                        blocks = [b["text"] for b in content if b["type"] == "text"]
                        mid = message.get("id") or object()   # no id: a message of its own
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                    if last is None:
                        if blocks:
                            last, texts = mid, blocks
                    elif mid != last:
                        break
                    else:
                        texts = blocks + texts
        except OSError as e:
            raise ConfigError(f"transcript {found[0]}: {e.strerror}") from e
        return "\n".join(texts)


RUNS = ClaudeClient.runs
