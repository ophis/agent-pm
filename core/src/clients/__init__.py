"""Clients: one class per agent client, turning a composed agent run into the command that starts it. Add one:
core/CLAUDE.md › Add a client.
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
