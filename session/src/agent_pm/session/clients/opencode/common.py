"""OpenCode's config checks and session bindings: each run's sid, under its cwd, bound to the
session id (`ses_…`) OpenCode chose, which a resume selects.
"""

import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping

from agent_pm.session.clients.common import CHECKS, FLAGS, MODES, check_config, mode_flags, scan
from agent_pm.session.engine.types import ClientContext, ConfigError, Effort, Permission, Spec
from agent_pm.utils import LOCAL_ENV_VARS, STATE, drop_stale, read_beneath, save

TABLE = "[clients.opencode]"
STORE = f"{STATE}/sessions/opencode"  # under HOME, resolved per call
NATIVE = re.compile(r"ses_[0-9A-Za-z]{1,128}")
BINDING_KEYS = frozenset({"version", "cwd", "sid", "native"})
BINDING_MAX = 4096
OLLAMA_PORT = 11434
# Set by OpenCode for its children (a nested start's are never its own).
IDENTITY = ("OPENCODE", "OPENCODE_PID", "OPENCODE_TERMINAL")
# Config OpenCode merges over or beside the launch's own OPENCODE_CONFIG_CONTENT.
OVERRIDES = (
    "OPENCODE_PERMISSION",
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
)
UNSET = (*sorted(LOCAL_ENV_VARS), *IDENTITY, *OVERRIDES)
SELECTORS = ("-s", "--session", "-c", "--continue", "--fork")
RESERVED = (*SELECTORS, "--format", "--attach", "--dir")
PERMISSIONS = ("--auto", "--yolo", "--dangerously-skip-permissions")
VALUED = (
    "-m",
    "--model",
    "--agent",
    "--variant",
    "--format",
    "-f",
    "--file",
    "--title",
    "--attach",
    "-p",
    "--password",
    "-u",
    "--username",
    "--dir",
    "--port",
    "-s",
    "--session",
)
PORT = (
    "ollama_port",
    lambda v: type(v) is int and 0 < v < 65536,
    "an integer port 1–65535",
)


def refuse(flags: Iterable[str], banned: tuple[str, ...], where: str) -> None:
    for flag in scan(tuple(flags), {word: word for word in VALUED}):
        if flag.word in banned:
            raise ConfigError(f"{flag.arg}: set by the opencode client, not {where}")


def refuse_env(env: Mapping[str, str], where: str) -> None:
    if found := sorted(set(env) & set(OVERRIDES)):
        raise ConfigError(f"{where}: {found[0]} would override the opencode client's config")


class OpenCodeCommon:
    def __init__(self, config: Mapping, *, selector):
        self.selector = selector
        check_config(config, TABLE, (FLAGS, MODES, PORT, *CHECKS))
        self.flags = tuple(config.get("flags", ()))
        self.modes = {p: mode_flags(config, p) for p in Permission}
        refuse(self.flags, (*RESERVED, *PERMISSIONS), f"{TABLE}.flags")
        for permission, flags in self.modes.items():
            refuse(flags, RESERVED, f"{TABLE}.modes.{permission}")
        self.port = config.get("ollama_port", OLLAMA_PORT)
        self.tiers = dict(config.get("tiers", {}))
        self.efforts = dict(config.get("efforts", {}))
        self.env = dict(config.get("env", {}))
        refuse_env(self.env, f"{TABLE}.env")

    def model(self, tier: int | None) -> str | None:
        if tier is None:
            return None
        if (model := self.tiers.get(str(tier))) is None:
            raise ConfigError(f"no model for tier {tier} in {TABLE}.tiers")
        return model

    def variant(self, effort: Effort | None) -> str | None:
        if effort is None:
            return None
        if (value := self.efforts.get(effort)) is None:
            raise ConfigError(f"no variant for effort {str(effort)!r} in {TABLE}.efforts")
        return value

    def inline(self, spec: Spec, host_address: str) -> dict:
        """The launch's OpenCode config: the ollama provider on `host_address`, auto-update off, and
        shell calls asking, which a headless run refuses, but the run's pre-approved commands; dirs
        outside the cwd are its Access.dirs alone, and OpenCode's own config files stay unedited.
        """
        access = spec.access
        models = {
            model.removeprefix("ollama/"): {"name": model.removeprefix("ollama/")}
            for model in self.tiers.values()
            if model.startswith("ollama/")
        }
        commands = dict.fromkeys(access.commands, "allow")
        permission: dict = {
            "bash": {"*": "ask", **commands} if commands else "ask",
            "edit": {".opencode/*": "deny", "opencode.json": "deny", "opencode.jsonc": "deny"},
        }
        dirs = [os.path.realpath(d) for d in access.dirs]
        if dirs:
            permission["external_directory"] = {
                pattern: "allow" for d in dirs for pattern in (d, f"{d}/*")
            }
        return {
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "provider": {
                "ollama": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Ollama",
                    "options": {"baseURL": f"http://{host_address}:{self.port}/v1"},
                    "models": models,
                }
            },
            "permission": permission,
        }

    def environment(self, spec: Spec, context: ClientContext) -> dict[str, str]:
        """The config's env, then Access's and the Spec's, under the client's own."""
        access = spec.access
        supplied = {**access.env, **spec.env}
        refuse_env(supplied, "a run's env")
        env = {k: v for k, v in {**self.env, **supplied}.items() if k not in LOCAL_ENV_VARS}
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.inline(spec, context.host_address))
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        if not access.project:
            env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        return env

    @property
    def store(self) -> str:
        return os.path.join(os.path.realpath(os.path.expanduser("~")), STORE.removeprefix("~/"))

    def _binding(self, cwd: str, sid: str) -> str:
        key = hashlib.sha256(f"{os.path.realpath(cwd)}\0{sid}".encode()).hexdigest()
        return os.path.join(self.store, f"{key}.json")

    def bind(self, cwd: str, sid: str, native: str) -> None:
        if not NATIVE.fullmatch(native):
            raise ConfigError(f"invalid OpenCode session id {native!r:.80}")
        record = {"version": 1, "cwd": os.path.realpath(cwd), "sid": sid, "native": native}
        try:
            os.makedirs(self.store, 0o700, exist_ok=True)
            save(self._binding(cwd, sid), json.dumps(record), durable=True)
        except OSError as error:
            raise ConfigError(f"opencode session binding: {error}") from None
        for error in drop_stale(self.store):
            print(f"opencode session binding: {error}", file=sys.stderr)

    def native(self, cwd: str, sid: str) -> str | None:
        """The session bound to (cwd, sid); None when none is (a symlink counts as none)."""
        path = self._binding(cwd, sid)
        data = read_beneath(self.store, path, limit=BINDING_MAX + 1)
        if data is None:
            return None
        try:
            record = json.loads(data) if len(data) <= BINDING_MAX else None
        except (ValueError, RecursionError):
            record = None
        if (
            not isinstance(record, dict)
            or frozenset(record) != BINDING_KEYS
            or record["version"] != 1
            or record["cwd"] != os.path.realpath(cwd)
            or record["sid"] != sid
            or not isinstance(record["native"], str)
            or not NATIVE.fullmatch(record["native"])
        ):
            raise ConfigError(f"{path}: not an opencode session binding for run ID {sid!r}")
        return record["native"]
