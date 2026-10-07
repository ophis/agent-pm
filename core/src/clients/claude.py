"""Claude Code: starts the agent run with `claude -p`, its data from [clients.claude] in config.toml. The run
reports progress and its outcome with the driver's report command; its stream-json output gives only the text to show.
"""
import json
import os
import re
import shlex
from collections.abc import Iterable, Iterator

from compose import ConfigError, RunConfig, RunParams, report_command

from .base import PROGRESS, Access, Client, Event, Launch


CORE_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS = os.path.expanduser("~/.claude/projects")
# This core's own plugin (<plugin>@<marketplace>): agent runs load the user's plugins (--setting-sources user), not it.
PLUGINS = {"enabledPlugins": {"agent-pm@agent-pm": False}}


def resume(cwd: str, sid: str, workdir: str) -> str:
    """The shell command a human resumes session `sid` with: claude in its cwd, reaching the workdir."""
    q = shlex.quote
    cmd = f"cd {q(cwd)} && claude --resume {q(sid)}"
    return cmd if os.path.realpath(cwd) == os.path.realpath(workdir) else f"{cmd} --add-dir {q(workdir)}"


def transcript(cwd: str, sid: str, projects: str = PROJECTS) -> str:
    """Where Claude Code saves session `sid` started in `cwd`: a folder named for cwd's real path, each
    non-alphanumeric character turned into "-"."""
    return os.path.join(projects, re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd)), f"{sid}.jsonl")


class ClaudeClient(Client):
    keys = frozenset({"flags", "tiers", "efforts", "env", "allow", "roles"})

    def __init__(self, config: dict):
        super().__init__(config)
        if any(str(f).split("=")[0] == "--setting-sources" for f in config.get("flags", [])):
            raise ConfigError("--setting-sources is the driver's (trusted_dirs): remove it from [clients.claude].flags")

    def handover(self) -> str:
        return ("Report through `{{report}}`, a pre-approved shell command, never in a reply:\n\n"
                f"- **Progress:** at each `[{PROGRESS}:<name>] …` line in your steps, before calling the next tool, run "
                "`{{report}} progress <name> <your report>`, e.g. "
                "`{{report}} progress start <what that line asks you to report>`.\n"
                "- **Outcome:** as your last action, after everything else is done and any background work you "
                "started has finished, run `{{report}} outcome --status <done|needs_input|failed> --title <one line> "
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
        stop = report_command(CORE_SCRIPTS, params) + " stop --pending background_tasks"
        hook = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": stop}]}]}}
        interactive = ["claude", prompt, *head, "--settings", json.dumps({**PLUGINS, **hook}), *tail]
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
