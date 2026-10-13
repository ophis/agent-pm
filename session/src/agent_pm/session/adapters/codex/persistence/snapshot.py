"""Codex invocation flags and recovery snapshot validation for isolated runtime helpers."""

from __future__ import annotations

import os
import re
import runpy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_pm.session.adapters.codex.persistence.types import RecoveryError

_load_file = runpy.run_path(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../hosts/tmux/loading/types.py")
)["load_file"]
if not TYPE_CHECKING:
    RecoveryError = _load_file(
        "agent_pm.session.adapters.codex.persistence.types",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "types.py"),
    ).RecoveryError


ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


VALUED = {
    "-c": "-c",
    "--config": "-c",
    "-C": "-C",
    "--cd": "-C",
    "-p": "-p",
    "--profile": "-p",
    "--enable": "--enable",
    "--disable": "--disable",
    "--add-dir": "--add-dir",
}


def split_flags(flags) -> tuple[list[str], list[str]]:
    index = flags.index("--") if "--" in flags else len(flags)
    return list(flags[:index]), list(flags[index:])


def options(flags):
    i = 0
    while i < len(flags) and flags[i] != "--":
        flag = flags[i]
        if flag.startswith("--"):
            word, eq, value = flag.partition("=")
        else:
            word, value = flag[:2], flag[2:].removeprefix("=")
            eq = flag[2:]
        name = VALUED.get(word)
        if name is None:
            yield (word if flag.startswith("--") else flag), None, flag
        elif eq:
            yield name, value, flag
        else:
            i += 1
            value = flags[i] if i < len(flags) else ""
            yield name, value, f"{flag} {value}"
        i += 1


def added_dirs(flags, cwd: str) -> list[str]:
    return [
        os.path.join(cwd, value)
        for name, value, _ in options(flags)
        if name == "--add-dir" and value is not None
    ]


def absolute_path(path: object, label: str) -> str:
    if not isinstance(path, str) or not path or not os.path.isabs(path) or "\x00" in path:
        raise RecoveryError.absolute_path(label)
    return os.path.realpath(path)


def validate_snapshot(value: dict) -> dict:
    if not isinstance(value, dict):
        raise RecoveryError.snapshot_field("fields")
    if set(value) != {
        "cwd",
        "project",
        "dirs",
        "commands",
        "model",
        "effort",
        "codex_home",
        "flags",
        "env",
    }:
        raise RecoveryError.snapshot_field("fields")
    env = value["env"]
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and ENV_NAME.fullmatch(k) and isinstance(v, str) and "\0" not in v
        for k, v in env.items()
    ):
        raise RecoveryError.snapshot_field("env")
    flags = value["flags"]
    if not isinstance(flags, list) or any(not isinstance(v, str) or "\0" in v for v in flags):
        raise RecoveryError.snapshot_field("flags")
    paths = {key: absolute_path(value[key], key) for key in ("cwd", "codex_home")}
    if type(value["project"]) is not bool:
        raise RecoveryError.snapshot_field("project")
    for key in ("dirs", "commands"):
        if not isinstance(value[key], list):
            raise RecoveryError.snapshot_field(key)
    dirs = [absolute_path(item, "dir") for item in value["dirs"]]
    commands = value["commands"]
    if any(not isinstance(item, str) or not item or not item.isprintable() for item in commands):
        raise RecoveryError.snapshot_field("commands")
    for key in ("model", "effort"):
        if value[key] is not None and (
            not isinstance(value[key], str) or not value[key] or not value[key].isprintable()
        ):
            raise RecoveryError.snapshot_field(key)
    validated = {
        **paths,
        "project": value["project"],
        "dirs": dirs,
        "commands": commands.copy(),
        "model": value["model"],
        "effort": value["effort"],
        "flags": list(flags),
        "env": dict(env),
    }
    if validated != value:
        raise RecoveryError.snapshot_field("path")
    return validated
