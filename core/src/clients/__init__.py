"""Clients: one module per agent client, turning a composed agent run into its launch command."""
import importlib
import re
import tomllib
from types import ModuleType

from compose import ConfigError, catalog as role_catalog

from .base import PROGRESS, Access, Client, Event, Launch, load_config  # noqa: F401

NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


def load_module(name: str) -> ModuleType:
    """Load the client's config-independent run capability before reading operator config."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ConfigError(f"unsafe client name {name!r}: want a lowercase module identifier")
    module_name = f"{__name__}.{name}"
    try:
        module = importlib.import_module(f".{name}", __name__)
    except ModuleNotFoundError as e:
        if e.name == module_name:
            raise ConfigError(f"client {name!r} has no module {module_name}") from e
        raise ConfigError(f"cannot import client {name!r}: {e}") from e
    except (ImportError, OSError) as e:
        raise ConfigError(f"cannot import client {name!r}: {e}") from e
    if type(getattr(module, "RUNS", None)) is not bool:
        raise ConfigError(f"client module {module_name} must declare boolean RUNS")
    return module


def get(name: str, root: str, *, module: ModuleType | None = None) -> Client:
    """The client named `name`, built from config.toml's [clients.<name>]."""
    try:
        cls, table = definition(name, root, module=module)
        return cls(table if cls.needs_config else {})
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"client {name!r} config: {e}") from e


def definition(name: str, root: str, *, module: ModuleType | None = None) -> tuple[type[Client], dict]:
    """Resolve the configured class, requiring its capability to match the module's declaration."""
    module = module if module is not None else load_module(name)
    table = dict(load_config(name, root))
    class_name = table.pop("implementation", None)
    if not isinstance(class_name, str) or not class_name.isidentifier():
        raise ConfigError(f"[clients.{name}].implementation must name a class in clients.{name}")
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ConfigError(f"client {name!r} module has no {class_name} class")
    if not isinstance(cls, type) or cls is Client or not issubclass(cls, Client):
        raise ConfigError(f"client {name!r} {class_name} must be a Client subclass")
    if cls.runs is not module.RUNS:
        raise ConfigError(f"client {name!r} {class_name}.runs disagrees with module RUNS")
    return cls, table


def catalog(name: str, root: str) -> dict[str, dict[str, str]]:
    """The roles and tasks available to a selected, configured client."""
    get(name, root)
    return role_catalog(root)
