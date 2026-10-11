"""Codex CLI headless client. Reports are still carried only by report.py."""
import json
import os
from collections.abc import Iterable, Iterator

from compose import ConfigError, RunConfig, RunParams

from .base import PROGRESS, Access, Client, Event, Launch


class CodexClient(Client):
    keys = frozenset({"tiers", "efforts"})

    def __init__(self, config: dict):
        super().__init__(config)
        for name, label in (("tiers", "tier"), ("efforts", "effort")):
            values = config.get(name)
            if not isinstance(values, dict):
                raise ConfigError(f"[clients.codex].{name} must be a table")
            for key, value in values.items():
                if not isinstance(value, str) or not value.strip() or not value.isprintable():
                    raise ConfigError(f"[clients.codex] {label} {key!r} must map to printable text")

    def handover(self) -> str:
        return ("Report through `report`, never in a final reply:\n\n"
                f"- **Progress:** at each `[{PROGRESS}:<name>] …` line in your steps, before calling the next tool, "
                "run `report progress <name> <your report>`.\n"
                "- **Outcome:** as your last action, after all background work has finished, run "
                "`report outcome --status <done|needs_input|failed> --title <one line> --summary <text> "
                "[--question <q>]... [--url <url>]... [--file <path>]... [--deliverable <file>]`. "
                "If it fails, fix it and run it again. A final reply is not an outcome.")

    def launch(self, prompt: str, run: RunConfig, *, params: RunParams, access: Access) -> Launch:
        if params.resume:
            raise ConfigError("Codex resume is not supported yet")
        tiers, efforts = self.config["tiers"], self.config["efforts"]
        model, effort = tiers.get(str(run.tier)), efforts.get(run.effort)
        if model is None:
            raise ConfigError(f"no model for tier {run.tier} in [clients.codex] in config.toml")
        if effort is None:
            raise ConfigError(f"no effort for {run.effort!r} in [clients.codex] in config.toml")
        workdir = os.path.realpath(os.path.abspath(params.workdir))
        cwd = os.path.abspath(access.cwd or params.workdir)
        real_cwd = os.path.realpath(cwd)
        if not access.project and os.path.commonpath((workdir, real_cwd)) != workdir:
            raise ConfigError(f"untrusted cwd {cwd} is outside the workdir: Codex project settings may load")
        argv = ["codex", "exec", "--json", "--sandbox", "workspace-write", "--skip-git-repo-check",
                "-m", model, "-c", f"model_reasoning_effort={effort}"]
        if not access.project:
            argv.extend(("-c", "project_doc_max_bytes=0"))
        for directory in access.dirs:
            argv.extend(("--add-dir", directory))
        argv.append(prompt)
        return Launch(argv, cwd=cwd)

    def events(self, lines: Iterable[str]) -> Iterator[Event]:
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                yield Event("stderr", "codex: invalid JSON event")
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "thread.started":
                sid = event.get("thread_id")
                if isinstance(sid, str) and sid:
                    yield Event("text", f"Codex session {sid} started")
            elif kind == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    message = item.get("text")
                    if isinstance(message, str):
                        yield from (Event("text", text) for text in message.splitlines() if text.strip())
            elif kind in ("error", "turn.failed"):
                error = event.get("message") if kind == "error" else event.get("error")
                if isinstance(error, dict):
                    error = error.get("message")
                if isinstance(error, str) and error.strip():
                    yield Event("stderr", error)
