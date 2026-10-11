"""The adapter registry, the only reader of the core config's [adapters] table: config.toml (CONFIG) with
~/.agent-pm/core-new.local.toml (LOCAL) merged on top. The tables are read once per process, on the first use of
configured() or adapter(), and cached (a failed read is not, so the next use reads again); configure() sets the cache
from a given config instead (tests), reset() drops it. adapter(name) imports package adapters.<name> and returns the
class its [adapters.<name>].implementation names, "adapters.<name>:<Class>Adapter", built from the table's other keys.
Every failure is a ConfigError."""
import importlib
import inspect
import os
import re
import threading
import tomllib
from collections.abc import Mapping

from adapters.base import Adapter, ConfigError

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG = os.path.join(ROOT, "config.toml")
LOCAL = "~/.agent-pm/core-new.local.toml"
NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
IMPLEMENTATION = re.compile(r"adapters\.(?P<name>[a-z][a-z0-9_]*):(?P<cls>[A-Za-z_][A-Za-z0-9_]*Adapter)\Z")
_lock = threading.Lock()
_cache: dict[str, dict] | None = None


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


def configure(config: Mapping) -> None:
    """Sets the cache to the [adapters] of the whole config `config` ({} when absent or not a table); reads no file."""
    global _cache
    found = config.get("adapters")
    with _lock:
        _cache = found if isinstance(found, dict) else {}


def reset() -> None:
    """Drops the cache: the next use reads the files."""
    global _cache
    with _lock:
        _cache = None


def _tables() -> dict[str, dict]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = tables(CONFIG, LOCAL)
        return _cache


def configured() -> list[str]:
    return sorted(_tables())


def adapter(name: str) -> Adapter:
    """The Adapter of runtime `name`, from its [adapters.<name>] table."""
    if not isinstance(name, str) or not NAME.match(name):
        raise ConfigError(f"unsafe runtime name {name!r}: want a lowercase identifier")
    table = _tables().get(name)
    if not isinstance(table, dict):
        raise ConfigError(f"no [adapters.{name}] table in {CONFIG} or {LOCAL}")
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
    except (ImportError, OSError, SyntaxError) as e:
        raise ConfigError(f"cannot import adapter {name!r}: {e}") from e
    try:
        cls = getattr(module, found["cls"])
    except AttributeError:
        raise ConfigError(f"{package} has no {found['cls']}") from None
    if not isinstance(cls, type) or cls is Adapter or not issubclass(cls, Adapter) or inspect.isabstract(cls):
        raise ConfigError(f"{package}:{found['cls']} must be a concrete Adapter subclass")
    return cls(config)
