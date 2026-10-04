"""Clients: one class per agent client, turning a composed run into the command that starts it.

A client gets the prompt, the run config and neutral Access (extra dirs, exact commands) and returns a Launch: a
command to start, files to write, or both. Its data (model names, effort names, fixed flags, env) comes from
clients/<name>.toml (core/clients/) when it needs any; its behavior is code.
Add a client: a module here with a Client subclass, registered in REGISTRY, plus clients/<name>.toml if it
needs data.
"""
from compose import ConfigError

from .base import Client, Launch, load_config  # noqa: F401
from .claude import ClaudeClient
from .skill import SkillClient

REGISTRY = {"claude": ClaudeClient, "skill": SkillClient}


def get(name, root):
    """The client named `name`, built from its clients/<name>.toml."""
    if name not in REGISTRY:
        raise ConfigError(f"unknown client {name!r} (known: {', '.join(sorted(REGISTRY))})")
    cls = REGISTRY[name]
    return cls(load_config(name, root) if cls.needs_config else {})
