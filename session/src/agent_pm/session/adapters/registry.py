"""Construct adapters from caller-owned configuration and validate their host settings."""

import copy
import re
from collections.abc import Mapping

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.adapters.types import Settings, TuiSettings
from agent_pm.session.engine.types import (
    ConfigError,
)
from agent_pm.session.hosts import tables
from agent_pm.session.hosts.tables import NAME
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.hosts.types import HostsError
from agent_pm.utils import load_class as load

PACKAGE = "agent_pm.session"
IMPLEMENTATION = re.compile(r"adapters\.(?P<name>[^:]+):(?P<cls>[A-Za-z_][A-Za-z0-9_]*Adapter)\Z")
TUI_KEYS = ("status_line", "workers_per_column", "grid_retile")


def settings(config: Mapping) -> dict:
    if not isinstance(config, Mapping):
        raise HostsError("session config: want a table")
    found = tables.parse(config.get("hosts"))
    return {
        "adapters": config.get("adapters", {}),
        "hosts": found.tables,
        "default_host": found.default,
    }


class Registry:
    """Caller-owned adapter and host configuration."""

    def __init__(self, *, adapters: Mapping, hosts: Mapping, default_host: str) -> None:
        if not isinstance(adapters, Mapping) or not all(
            isinstance(k, str) and isinstance(t, Mapping) for k, t in adapters.items()
        ):
            raise ConfigError("adapters: want a mapping of [adapters.<name>] tables")
        if not isinstance(hosts, Mapping) or "default" in hosts:
            raise ConfigError("hosts: want a mapping of [hosts.<name>] tables")
        try:
            found = tables.parse({**hosts, "default": default_host})
        except ValueError as e:
            raise ConfigError(str(e)) from None
        settings = Settings(
            copy.deepcopy({k: dict(t) for k, t in adapters.items()}), found.tables, found.default
        )
        self.settings = settings

    def configured(self) -> list[str]:
        return sorted(self.settings.adapters)

    def tui(self) -> TuiSettings:
        table = self.settings.hosts.get(tmux_host.HOST, {})
        line, per, retile = (table.get(k) for k in TUI_KEYS)
        if line is not None and not isinstance(line, bool):
            raise ConfigError("status_line: want true or false")
        if per is not None and (
            isinstance(per, bool) or not isinstance(per, int) or not 1 <= per <= 9999
        ):
            raise ConfigError("workers_per_column: want an integer from 1 to 9999")
        if retile is not None and (not isinstance(retile, str) or retile not in tmux_host.RETILES):
            raise ConfigError(f"grid_retile: want one of {', '.join(tmux_host.RETILES)}")
        return TuiSettings(bool(line), per, retile)

    def adapter(self, name: str) -> Adapter:
        if not isinstance(name, str) or not NAME.match(name):
            raise ConfigError(f"unsafe runtime name {name!r}: want a lowercase identifier")
        table = self.settings.adapters.get(name)
        if table is None:
            raise ConfigError(f"no [adapters.{name}] table in the configured adapters")
        config = dict(table)
        implementation = config.pop("implementation", None)
        found = IMPLEMENTATION.match(implementation) if isinstance(implementation, str) else None
        if not found or found["name"] != name:
            raise ConfigError(
                f'[adapters.{name}].implementation must be "adapters.{name}:<Class>Adapter"'
            )
        try:
            cls = load(f"{PACKAGE}.adapters.{name}:{found['cls']}", Adapter)
        except ValueError as e:
            raise ConfigError(f"adapter {name!r}: {e}") from e
        return cls(config)
