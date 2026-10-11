"""Clients: one module per agent client, turning a composed agent run into its launch command."""
import importlib
import os
import re

import repo
from compose import ConfigError
from compose import CONFIG

from .base import PROGRESS, Access, Client, Event, Launch, load_config  # noqa: F401
from .claude import ClaudeClient
from .skill import SkillClient

NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


def names(root: str) -> tuple[str, ...]:
    """Configured client names in config order, including the merged local file."""
    path = os.path.join(root, CONFIG)
    if not os.path.isfile(path):
        raise ConfigError(f"no core config {path}")
    tables = repo.read_config(path).get("clients")
    if not isinstance(tables, dict) or any(not isinstance(table, dict) for table in tables.values()):
        raise ConfigError(f"malformed [clients] table in {path}")
    return tuple(tables)


def get(name: str, root: str) -> Client:
    """The client named `name`, built from config.toml's [clients.<name>]."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ConfigError(f"unsafe client name {name!r}: want a lowercase module identifier")
    table = load_config(name, root)
    module_name = f"{__name__}.{name}"
    try:
        module = importlib.import_module(f".{name}", __name__)
    except ModuleNotFoundError as e:
        if e.name != module_name:
            raise
        raise ConfigError(f"client {name!r} has no module {module_name}") from e
    class_name = "".join(part.capitalize() for part in name.split("_")) + "Client"
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ConfigError(f"client {name!r} module has no {class_name} class")
    if not isinstance(cls, type) or not issubclass(cls, Client):
        raise ConfigError(f"client {name!r} {class_name} must be a Client subclass")
    return cls(table if cls.needs_config else {})
