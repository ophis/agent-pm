"""The adapter registry, the only reader of the core config's [adapters] tables and tui keys (TUI_KEYS) (config.toml,
LOCAL merged on top), read once per process and cached (a failed read is not); configure() sets the cache instead,
reset() drops it. adapter(name) builds the class [adapters.<name>].implementation names from the table's other keys.
Every failure is a ConfigError."""
import importlib
import inspect
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from adapters.base import Adapter, ConfigError
from driver import tmux_host
from utils.config_file import CONFIG, LOCAL, read_config

NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
IMPLEMENTATION = re.compile(r"adapters\.(?P<name>[a-z][a-z0-9_]*):(?P<cls>[A-Za-z_][A-Za-z0-9_]*Adapter)\Z")
TUI_KEYS = ("status_line", "workers_per_column", "grid_retile")   # global keys
_lock = threading.Lock()
_cache: dict | None = None


@dataclass(frozen=True)
class TuiSettings:
    """The tui display settings: tmux's status line, a grid's panes per column, when it re-tiles (None: tmux_host's)."""
    status_line: bool = False
    per_column: int | None = None
    retile: str | None = None


def read(path: str = CONFIG, local: str = LOCAL) -> dict:
    try:
        return _kept(read_config(path, local))
    except (OSError, ValueError) as e:
        raise ConfigError(f"core config {path}: {e}") from e


def _kept(config: Mapping) -> dict:
    found = config.get("adapters")
    return {"adapters": found if isinstance(found, dict) else {}, **{k: config[k] for k in TUI_KEYS if k in config}}


def tables(path: str = CONFIG, local: str = LOCAL) -> dict[str, dict]:
    return read(path, local)["adapters"]


def configure(config: Mapping) -> None:
    global _cache
    with _lock:
        _cache = _kept(config)


def reset() -> None:
    global _cache
    with _lock:
        _cache = None


def _config() -> dict:
    global _cache
    with _lock:
        if _cache is None:
            _cache = read(CONFIG, LOCAL)
        return _cache


def configured() -> list[str]:
    return sorted(_config()["adapters"])


def tui() -> TuiSettings:
    """The cached tui keys; a value of the wrong type or range is a ConfigError naming its key."""
    line, per, retile = (_config().get(k) for k in TUI_KEYS)
    if line is not None and not isinstance(line, bool):
        raise ConfigError("status_line: want true or false")
    if per is not None and (isinstance(per, bool) or not isinstance(per, int) or not 1 <= per <= 9999):
        raise ConfigError("workers_per_column: want an integer from 1 to 9999")
    if retile is not None and (not isinstance(retile, str) or retile not in tmux_host.RETILES):
        raise ConfigError(f"grid_retile: want one of {', '.join(tmux_host.RETILES)}")
    return TuiSettings(bool(line), per, retile)


def adapter(name: str) -> Adapter:
    if not isinstance(name, str) or not NAME.match(name):
        raise ConfigError(f"unsafe runtime name {name!r}: want a lowercase identifier")
    table = _config()["adapters"].get(name)
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
