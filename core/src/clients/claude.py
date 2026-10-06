"""Claude Code: starts the agent run with `claude -p`, its data from [clients.claude] in config/config.toml. The run
reports progress and its outcome with the driver's report command; its stream-json output gives only the text to show.
"""
import json
import os
from collections.abc import Iterable, Iterator

from compose import ConfigError, RunConfig, RunParams, report_command

from .base import PROGRESS, Access, Client, Event, Launch


CORE_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ClaudeClient(Client):
    keys = frozenset({"flags", "tiers", "efforts", "env", "allow", "roles"})

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
            raise ConfigError(f"no model for tier {run.tier} in [clients.claude] in config/config.toml")
        if effort is None:
            raise ConfigError(f"no effort for {run.effort!r} in [clients.claude] in config/config.toml")
        head = ["--resume" if params.resume else "--session-id", params.sid, "--model", model, "--effort", effort,
                *c.get("flags", [])]
        tail = []
        for d in access.dirs:
            tail += ["--add-dir", d]
        allow = [f"Bash({cmd})" for cmd in access.commands] + (self.value(run, "allow") or [])
        if allow:
            tail += ["--allowedTools", *allow]
        argv = ["claude", "-p", prompt, *head, "--output-format", "stream-json", "--verbose", *tail]
        stop = report_command(CORE_SCRIPTS, params) + " stop --pending background_tasks"
        hook = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": stop}]}]}}
        interactive = ["claude", prompt, *head, *tail, "--settings", json.dumps(hook)]
        return Launch(argv, dict(c.get("env", {})), cwd=os.path.abspath(params.workdir), interactive=interactive)

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
