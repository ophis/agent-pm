"""Codex CLI headless client. Reports are still carried only by report.py."""
import json
import glob
import os
import shlex
import stat
import sys
import uuid
from collections.abc import Iterable, Iterator

import run_session
from compose import ConfigError, RunConfig, RunParams

from .base import PROGRESS, Access, Client, Event, Launch, SessionInfo


def _absolute(path: str, label: str) -> str:
    if not isinstance(path, str) or not path or not os.path.isabs(path) or "\x00" in path:
        raise ConfigError(f"invalid Codex recovery {label}: expected an absolute path")
    return os.path.realpath(path)


def _snapshot(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"cwd", "project", "dirs", "commands", "model", "effort",
                                                   "network", "codex_home"}:
        raise ConfigError("invalid Codex recovery snapshot fields")
    paths = {key: _absolute(value[key], key) for key in ("cwd", "codex_home")}
    for key in ("project", "network"):
        if type(value[key]) is not bool:
            raise ConfigError(f"invalid Codex recovery snapshot {key}")
    for key in ("dirs", "commands"):
        if not isinstance(value[key], list):
            raise ConfigError(f"invalid Codex recovery snapshot {key}")
    dirs = [_absolute(item, "dir") for item in value["dirs"]]
    commands = value["commands"]
    if any(not isinstance(item, str) or not item or not item.isprintable() for item in commands):
        raise ConfigError("invalid Codex recovery snapshot commands")
    for key in ("model", "effort"):
        if not isinstance(value[key], str) or not value[key] or not value[key].isprintable():
            raise ConfigError(f"invalid Codex recovery snapshot {key}")
    validated = {**paths, "project": value["project"], "dirs": dirs, "commands": commands.copy(),
                 "model": value["model"], "effort": value["effort"], "network": value["network"]}
    if validated != value:
        raise ConfigError("invalid Codex recovery snapshot path")
    return validated


def _inside(path: str, root: str) -> bool:
    return os.path.commonpath((path, root)) == root


def _guard_untrusted_project(workdir: str, cwd: str, config_home: str | None = None) -> None:
    """Keep project-controlled Codex configuration out of an untrusted run."""
    real_workdir, real_cwd = os.path.realpath(workdir), os.path.realpath(cwd)
    if not _inside(real_cwd, real_workdir):
        raise ConfigError(f"untrusted cwd {cwd} is outside the workdir: Codex project settings may load")

    default_home = os.path.abspath(os.path.expanduser("~/.codex"))
    real_default_home = os.path.realpath(default_home)
    config_home = config_home or os.environ.get("CODEX_HOME") or default_home
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
                                     and os.path.realpath(config_home) == real_default_home
                                     and real_candidate == real_default_home
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


def _codex_home(cwd: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.join(
        cwd, os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))))


def _no_symlink_ancestors(path: str) -> None:
    current = os.path.abspath(path)
    while True:
        if os.path.islink(current):
            raise ConfigError(f"unsafe Codex session file symlink: {current}")
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _session_file(snapshot: dict, client_id: str) -> tuple[str, float] | None:
    """Find this exact native session, then verify the CLI's own first metadata event."""
    try:
        if str(uuid.UUID(client_id)) != client_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigError("invalid Codex recovery client ID") from exc
    root = os.path.join(snapshot["codex_home"], "sessions")
    _no_symlink_ancestors(root)
    matches = glob.glob(os.path.join(root, "[0-9]" * 4, "[0-9]" * 2, "[0-9]" * 2,
                                     f"rollout-*-{client_id}.jsonl"))
    if len(matches) > 1:
        raise ConfigError(f"duplicate Codex session files for client ID {client_id}")
    if not matches:
        return None
    path = matches[0]
    _no_symlink_ancestors(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ConfigError(f"cannot read Codex session file {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ConfigError(f"unsafe Codex session file: {path}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            try:
                metadata = json.loads(stream.readline(128 * 1024))
            except (ValueError, UnicodeError) as exc:
                raise ConfigError(f"malformed Codex session metadata: {path}") from exc
        if (not isinstance(metadata, dict) or metadata.get("type") != "session_meta"
                or not isinstance(metadata.get("payload"), dict)
                or metadata["payload"].get("id") != client_id
                or metadata["payload"].get("cwd") != snapshot["cwd"]):
            raise ConfigError(f"mismatched Codex session metadata for client ID {client_id}: {path}")
        return path, info.st_mtime
    finally:
        if fd >= 0:
            os.close(fd)


def _flags(snapshot: dict, *, interactive: bool) -> list[str]:
    flags = ["--approve-for-me", "-m", snapshot["model"],
             "-c", f"model_reasoning_effort={snapshot['effort']}",
             "-c", f"sandbox_workspace_write.network_access={str(snapshot['network']).lower()}"]
    if not snapshot["project"]:
        flags.extend(("-c", "project_doc_max_bytes=0"))
    for directory in snapshot["dirs"]:
        flags.extend(("--add-dir", directory))
    if not interactive:
        flags[:0] = ["--json", "--skip-git-repo-check"]
    return flags


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
            state = run_session.load(params.workdir, params.sid)
            if state is None or state.client != "codex":
                raise ConfigError(f"missing Codex recovery state for run ID {params.sid!r}")
            if state.client_id is None:
                raise ConfigError(f"missing Codex client ID for run ID {params.sid!r}")
            snapshot = _snapshot(state.snapshot)
            if not _session_file(snapshot, state.client_id):
                raise ConfigError(f"missing Codex session file for client ID {state.client_id}")
            if not snapshot["project"]:
                _guard_untrusted_project(state.workdir, snapshot["cwd"], snapshot["codex_home"])
            argv = ["codex", "exec", *_flags(snapshot, interactive=False),
                    "resume", state.client_id, prompt]
            return Launch(argv, env={"CODEX_HOME": snapshot["codex_home"]}, cwd=snapshot["cwd"],
                          session_state=snapshot, writable_roots=state.writable_roots)
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
        codex_home = _codex_home(cwd)
        print(f"drive.py: Codex tier {run.tier} uses {model}; "
              "update [clients.codex.tiers] in ~/.agent-pm/core.local.toml when models change",
              file=sys.stderr)
        if access.commands:
            print("drive.py: configured commands are subject to Codex approval; they are not preapproved",
                  file=sys.stderr)
        snapshot = {"cwd": os.path.realpath(cwd), "project": access.project,
                    "dirs": [os.path.realpath(os.path.abspath(directory)) for directory in access.dirs],
                    "commands": access.commands.copy(), "model": model, "effort": effort,
                    "network": self.config["shell_network"], "codex_home": codex_home}
        argv = ["codex", "exec", *_flags({**snapshot, "dirs": access.dirs}, interactive=False)]
        argv.append(prompt)
        return Launch(argv, env={"CODEX_HOME": codex_home}, cwd=cwd, session_state=_snapshot(snapshot),
                      writable_roots=[os.path.realpath(workdir), snapshot["cwd"], codex_home, *snapshot["dirs"]])

    def resume_location(self, workdir: str, sid: str) -> tuple[str, bool]:
        state = run_session.load(workdir, sid)
        if state is None or state.client != "codex":
            raise ConfigError(f"missing Codex recovery state for run ID {sid!r}")
        snapshot = _snapshot(state.snapshot)
        return snapshot["cwd"], snapshot["project"]

    def session_info(self, workdir: str, sid: str) -> SessionInfo:
        state = run_session.load(workdir, sid)
        if state is None or state.client != "codex":
            return SessionInfo(resume_supported=True, inspection_supported=True)
        snapshot = _snapshot(state.snapshot)
        if state.client_id is None:
            return SessionInfo(resume_supported=True, inspection_supported=True)
        found = _session_file(snapshot, state.client_id)
        if found is None:
            return SessionInfo(resume_supported=True, inspection_supported=True, client_id=state.client_id)
        path, activity = found
        command = ["codex", "resume", *_flags(snapshot, interactive=True), state.client_id]
        manual = f"cd {shlex.quote(snapshot['cwd'])} && CODEX_HOME={shlex.quote(snapshot['codex_home'])} " \
                 + shlex.join(command)
        return SessionInfo(resume_supported=True, inspection_supported=True, client_id=state.client_id,
                           exists=True, transcript=path, last_activity=activity, resume=manual)

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
                try:
                    if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid:
                        raise ValueError
                except ValueError:
                    yield Event("stderr", "codex: invalid thread.started ID")
                    continue
                yield Event("session", session_id=sid)
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
