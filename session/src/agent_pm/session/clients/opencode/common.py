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
from agent_pm.session.engine.types import ConfigError, Permission
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
