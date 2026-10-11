"""Codex CLI headless client. Reports are still carried only by report.py."""
import json
import os
import sys
from collections.abc import Iterable, Iterator

from compose import ConfigError, RunConfig, RunParams

from .base import PROGRESS, Access, Client, Event, Launch


def _inside(path: str, root: str) -> bool:
    return os.path.commonpath((path, root)) == root


def _guard_untrusted_project(workdir: str, cwd: str) -> None:
    """Keep project-controlled Codex configuration out of an untrusted run."""
    real_workdir, real_cwd = os.path.realpath(workdir), os.path.realpath(cwd)
    if not _inside(real_cwd, real_workdir):
        raise ConfigError(f"untrusted cwd {cwd} is outside the workdir: Codex project settings may load")

    default_home = os.path.abspath(os.path.expanduser("~/.codex"))
    real_default_home = os.path.realpath(default_home)
    config_home = os.environ.get("CODEX_HOME") or default_home
    config_home = os.path.abspath(os.path.join(cwd, config_home))
    real_config_home = os.path.realpath(config_home)
    if _inside(config_home, workdir) or _inside(real_config_home, real_workdir):
        raise ConfigError(f"CODEX_HOME {config_home} is inside the untrusted workdir {workdir}; "
                          "move Codex config outside the workdir")

    for start in (cwd, real_cwd):
        ancestor = start
        while True:
            candidate = os.path.join(ancestor, ".codex")
            if os.path.lexists(candidate) and (os.path.isdir(candidate) or os.path.islink(candidate)):
                real_candidate = os.path.realpath(candidate)
                genuine_user_home = (candidate in (default_home, real_default_home)
                                     and config_home == default_home and real_candidate == real_default_home
                                     and not os.path.islink(candidate)
                                     and not _inside(candidate, workdir)
                                     and not _inside(real_candidate, real_workdir))
                if not genuine_user_home:
                    raise ConfigError(f"project Codex settings at {candidate} may load in an untrusted run; "
                                      "use a clean workdir or trusted_dirs")
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                break
            ancestor = parent


class CodexClient(Client):
    keys = frozenset({"tiers", "efforts", "shell_network"})

    def __init__(self, config: dict):
        super().__init__(config)
        if type(config.get("shell_network")) is not bool:
            raise ConfigError("[clients.codex].shell_network must be a boolean")
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
                "[--question <q>]... [--url <url>] [--file <path>]... [--deliverable <file>]`. "
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
        workdir = os.path.abspath(params.workdir)
        cwd = os.path.abspath(access.cwd or params.workdir)
        if not access.project:
            _guard_untrusted_project(workdir, cwd)
        print(f"drive.py: Codex tier {run.tier} uses {model}; "
              "update [clients.codex.tiers] in ~/.agent-pm/core.local.toml when models change",
              file=sys.stderr)
        if access.commands:
            print("drive.py: configured commands are subject to Codex approval; they are not preapproved",
                  file=sys.stderr)
        argv = ["codex", "exec", "--json", "--skip-git-repo-check",
                "--approve-for-me", "-m", model, "-c", f"model_reasoning_effort={effort}",
                "-c", f"sandbox_workspace_write.network_access={str(self.config['shell_network']).lower()}"]
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
