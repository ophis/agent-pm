"""Clients: one class per agent client, turning a composed run into the command that starts it.

A client gets the prompt, the run config and neutral Access (extra dirs, read-only dirs, exact commands) and returns a
Launch. Its data (model names, effort names, fixed flags, env) comes from drivers/<name>.toml; its behavior is code.
Add a client: subclass Client, register it in REGISTRY, add drivers/<name>.toml.
"""
import os
import tomllib
from dataclasses import dataclass, field

from compose import ConfigError


@dataclass
class Launch:
    argv: list
    env: dict = field(default_factory=dict)   # added to the caller's environment
    cwd: str = ""                             # set by the driver


class Client:
    keys = frozenset()
    needs_config = True

    def __init__(self, config):
        if extra := sorted(set(config) - self.keys):
            raise ConfigError(f"unknown key {extra[0]!r} in {type(self).__name__}'s config")
        self.config = config

    def launch(self, prompt, run, *, sid, resume, access):
        raise NotImplementedError


class ClaudeClient(Client):
    keys = frozenset({"flags", "tiers", "efforts", "env", "tasks"})

    def launch(self, prompt, run, *, sid, resume, access):
        c = self.config
        model, effort = c.get("tiers", {}).get(str(run["tier"])), c.get("efforts", {}).get(run["effort"])
        if model is None:
            raise ConfigError(f"no model for tier {run['tier']} in drivers/claude.toml")
        if effort is None:
            raise ConfigError(f"no effort for {run['effort']!r} in drivers/claude.toml")
        argv = ["claude", "-p", prompt, "--resume" if resume else "--session-id", sid,
                "--model", model, "--effort", effort, *c.get("flags", [])]
        for d in access.dirs:
            argv += ["--add-dir", d]
        if access.read_only:
            argv += ["--disallowedTools", *(f"Edit(/{p}/**)" for p in access.read_only)]  # // marks an absolute path
        allow = [f"Bash({cmd})" for cmd in access.commands] + c.get("tasks", {}).get(run["task"], {}).get("allow", [])
        if allow:
            argv += ["--allowedTools", *allow]
        return Launch(argv, dict(c.get("env", {})))


REGISTRY = {"claude": ClaudeClient}


def load_config(name, root):
    path = os.path.join(root, "drivers", f"{name}.toml")
    if not os.path.isfile(path):
        raise ConfigError(f"no client config {path}")
    with open(path, "rb") as f:
        return tomllib.load(f)


def get(name, root):
    """The client named `name`, built from its drivers/<name>.toml."""
    if name not in REGISTRY:
        raise ConfigError(f"unknown client {name!r} (known: {', '.join(sorted(REGISTRY))})")
    cls = REGISTRY[name]
    return cls(load_config(name, root) if cls.needs_config else {})
