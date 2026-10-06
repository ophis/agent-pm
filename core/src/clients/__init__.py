"""Clients: one class per agent client, turning a composed agent run into the command that starts it.

A client gets the prompt and the run config and returns a Launch: a run client (runs = True) a command to start, from
launch() with the RunParams and neutral Access (extra dirs, commands to pre-approve); an export client the files to
write, from export(). Its data (model names, effort names, fixed flags, env) comes from
config.toml's [clients.<name>] when it needs any; its behavior is code.
Add a client: a module here with a Client subclass, registered in REGISTRY, plus [clients.<name>] in config.toml
if it needs data.
"""
from compose import ConfigError

from .base import PROGRESS, Access, Client, Event, Launch, load_config  # noqa: F401
from .claude import ClaudeClient
from .skill import SkillClient

REGISTRY = {"claude": ClaudeClient, "skill": SkillClient}


def get(name: str, root: str) -> Client:
    """The client named `name`, built from config.toml's [clients.<name>]."""
    if name not in REGISTRY:
        raise ConfigError(f"unknown client {name!r} (known: {', '.join(sorted(REGISTRY))})")
    cls = REGISTRY[name]
    return cls(load_config(name, root) if cls.needs_config else {})
