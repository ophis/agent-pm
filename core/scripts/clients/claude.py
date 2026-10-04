"""Claude Code: starts the run with `claude -p`, its data from config/clients/claude.toml. The outcome comes back as
structured output checked against the schema; progress as PROGRESS lines in its replies, both read from the stream-json
output."""
import json
import os
from collections.abc import Iterable, Iterator

from compose import ConfigError, RunConfig, RunParams

from .base import PROGRESS, PROGRESS_LINE, Access, Client, Event, Launch


class ClaudeClient(Client):
    keys = frozenset({"flags", "tiers", "efforts", "env", "allow", "roles"})

    def handover(self) -> str:
        return (f"Return the outcome as your structured output when you finish. At each `[{PROGRESS}:<name>]` point, "
                f"write a line in your reply starting with the same mark, your report after it, e.g. "
                f"`[{PROGRESS}:budget] ultracode only, cap 80`.")

    def launch(self, prompt: str, run: RunConfig, *, params: RunParams, access: Access, schema: dict) -> Launch:
        c = self.config
        model, effort = c.get("tiers", {}).get(str(run.tier)), c.get("efforts", {}).get(run.effort)
        if model is None:
            raise ConfigError(f"no model for tier {run.tier} in config/clients/claude.toml")
        if effort is None:
            raise ConfigError(f"no effort for {run.effort!r} in config/clients/claude.toml")
        argv = ["claude", "-p", prompt, "--resume" if params.resume else "--session-id", params.sid,
                "--model", model, "--effort", effort, *c.get("flags", []),
                "--output-format", "stream-json", "--verbose", "--json-schema", json.dumps(schema)]
        for d in access.dirs:
            argv += ["--add-dir", d]
        allow = [f"Bash({cmd})" for cmd in access.commands] + (self.value(run, "allow") or [])
        if allow:
            argv += ["--allowedTools", *allow]
        return Launch(argv, dict(c.get("env", {})), cwd=os.path.abspath(params.workdir))

    def events(self, lines: Iterable[str]) -> Iterator[Event]:
        # A resumed session emits an empty `result` first and system events after the real one: every result with
        # structured output is yielded, and the driver keeps the last.
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                yield Event("text", line.rstrip("\n"))
                continue
            if e.get("type") == "assistant":
                for block in e.get("message", {}).get("content", []):
                    if block.get("type") != "text":
                        continue
                    for line in block.get("text", "").splitlines():
                        if m := PROGRESS_LINE.match(line):
                            yield Event("progress", m.group(2).strip(), name=m.group(1))
                        elif line.strip():
                            yield Event("text", line)
            elif e.get("type") == "result" and isinstance(e.get("structured_output"), dict):
                yield Event("outcome", outcome=e["structured_output"])
