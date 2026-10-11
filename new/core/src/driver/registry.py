"""The adapter registry, the only reader of the core config's [adapters] table: config.toml (CONFIG) with
~/.agent-pm/core-new.local.toml (LOCAL) merged on top. adapter(name) imports package adapters.<name> and returns the
class its [adapters.<name>].implementation names, "adapters.<name>:<Class>Adapter", built from the table's other keys.
Every failure is a ConfigError."""
import importlib
import inspect
import os
import re
import tomllib

from adapters.base import Adapter, ConfigError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG = os.path.join(ROOT, "config.toml")
LOCAL = "~/.agent-pm/core-new.local.toml"
NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
IMPLEMENTATION = re.compile(r"adapters\.(?P<name>[a-z][a-z0-9_]*):(?P<cls>[A-Za-z_][A-Za-z0-9_]*Adapter)\Z")


def merge(base: dict, over: dict) -> dict:
    """`base` with `over` on top: tables merge key by key; any other value of `over` replaces base's."""
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read_config(path: str = CONFIG, local: str = LOCAL) -> dict:
    """TOML file `path`, with TOML file `local` (~ expanded) merged on top when it exists (merge())."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    local = os.path.expanduser(local)
    if os.path.isfile(local):
        with open(local, "rb") as f:
            cfg = merge(cfg, tomllib.load(f))
    return cfg


def tables(path: str = CONFIG, local: str = LOCAL) -> dict[str, dict]:
    """The merged config's [adapters]; {} when it is absent or not a table."""
    try:
        found = read_config(path, local).get("adapters")
    except (OSError, ValueError) as e:
        raise ConfigError(f"core config {path}: {e}") from e
    return found if isinstance(found, dict) else {}


def configured(path: str = CONFIG, local: str = LOCAL) -> list[str]:
    return sorted(tables(path, local))


def adapter(name: str, *, path: str = CONFIG, local: str = LOCAL) -> Adapter:
    """The Adapter of runtime `name`, from its [adapters.<name>] table."""
    if not isinstance(name, str) or not NAME.match(name):
        raise ConfigError(f"unsafe runtime name {name!r}: want a lowercase identifier")
    table = tables(path, local).get(name)
    if not isinstance(table, dict):
        raise ConfigError(f"no [adapters.{name}] table in {path} or {local}")
    config = dict(table)
    implementation = config.pop("implementation", None)
    found = IMPLEMENTATION.match(implementation) if isinstance(implementation, str) else None
    if not found or found["name"] != name:
        raise ConfigError(f'[adapters.{name}].implementation must be "adapters.{name}:<Class>Adapter"')
    package = f"adapters.{name}"
    try:
        module = importlib.import_module(package)
    except ModuleNotFoundError as e:
        if e.name in (package, "adapters"):
            raise ConfigError(f"adapter {name!r} has no package {package}") from e
        raise ConfigError(f"cannot import adapter {name!r}: {e}") from e
    except (ImportError, OSError) as e:
        raise ConfigError(f"cannot import adapter {name!r}: {e}") from e
    try:
        cls = getattr(module, found["cls"])
    except AttributeError:
        raise ConfigError(f"{package} has no {found['cls']}") from None
    if not isinstance(cls, type) or cls is Adapter or not issubclass(cls, Adapter) or inspect.isabstract(cls):
        raise ConfigError(f"{package}:{found['cls']} must be a concrete Adapter subclass")
    return cls(config)
