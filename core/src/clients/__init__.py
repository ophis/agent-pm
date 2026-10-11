"""Clients: one module per agent client, turning a composed agent run into its launch command."""
import importlib
import re

from compose import ConfigError, catalog as role_catalog

from .base import PROGRESS, Access, Client, Event, Launch, load_config  # noqa: F401
from .claude import ClaudeClient
from .skill import SkillClient

NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


def get(name: str, root: str) -> Client:
    """The client named `name`, built from config.toml's [clients.<name>]."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ConfigError(f"unsafe client name {name!r}: want a lowercase module identifier")
    table = dict(load_config(name, root))
    class_name = table.pop("implementation", None)
    if not isinstance(class_name, str) or not class_name.isidentifier():
        raise ConfigError(f"[clients.{name}].implementation must name a class in clients.{name}")
    module_name = f"{__name__}.{name}"
    try:
        module = importlib.import_module(f".{name}", __name__)
    except ModuleNotFoundError as e:
        if e.name != module_name:
            raise
        raise ConfigError(f"client {name!r} has no module {module_name}") from e
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ConfigError(f"client {name!r} module has no {class_name} class")
    if not isinstance(cls, type) or cls is Client or not issubclass(cls, Client):
        raise ConfigError(f"client {name!r} {class_name} must be a Client subclass")
    return cls(table if cls.needs_config else {})


def catalog(name: str, root: str) -> dict[str, dict[str, str]]:
    """The roles and tasks available to a selected, configured client."""
    get(name, root)
    return role_catalog(root)
