"""Caller-owned adapter configuration, copied read-only once: adapter_class loads a table's
`implementation` (module.path:Class); adapter builds one per call (per session) from its own copy.
Validates the host settings.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from agent_pm.session.adapters.base import Adapter
from agent_pm.session.engine.types import ConfigError, Hosts, TuiSettings
from agent_pm.session.hosts.base import NAME, Host
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.utils import freeze, thaw
from agent_pm.utils import load_class as load


@dataclass(frozen=True)
class Settings:
    adapters: Mapping[str, Mapping]
    hosts: Mapping[str, Mapping]
    default_host: str


PACKAGE = "agent_pm.session"
TUI_KEYS = ("status_line", "workers_per_column", "grid_retile")


def settings(config: Mapping, *, hosts: Hosts | None = None) -> dict:
    """A read session config's [adapters], and its [hosts] parsed once (`hosts`: parsed already)."""
    if not isinstance(config, Mapping):
        raise ConfigError("session config: want a table")
    if hosts is None:
        hosts = Host.parse_hosts(config.get("hosts"))
    return {"adapters": config.get("adapters", {}), "hosts": hosts}


class Registry:
    """`hosts` is already parsed."""

    def __init__(self, *, adapters: Mapping, hosts: Hosts, class_loader=load) -> None:
        if not isinstance(adapters, Mapping) or not all(
            isinstance(k, str) and isinstance(t, Mapping) for k, t in adapters.items()
        ):
            raise ConfigError("adapters: want a mapping of [adapters.<name>] tables")
        self.settings = Settings(freeze(adapters), hosts.tables, hosts.default)
        self.class_loader = class_loader

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
        if retile is not None and retile not in tuple(tmux_host.Retile):
            raise ConfigError(f"grid_retile: want one of {', '.join(tmux_host.Retile)}")
        return TuiSettings(bool(line), per, retile)

    def _table(self, name: str) -> Mapping:
        if not isinstance(name, str) or not NAME.match(name):
            raise ConfigError(f"unsafe runtime name {name!r}: want a lowercase identifier")
        table = self.settings.adapters.get(name)
        if table is None:
            raise ConfigError(f"no [adapters.{name}] table in the configured adapters")
        return table

    def adapter_class(self, name: str) -> type[Adapter]:
        path = self._table(name).get("implementation")
        where = f"[adapters.{name}].implementation"
        if isinstance(path, str) and path.startswith("adapters."):
            raise ConfigError(f"{where}: {path} is relative: want {PACKAGE}.{path}")
        try:
            return self.class_loader(path, Adapter)
        except ValueError as e:
            raise ConfigError(f"{where}: {e}") from None

    def adapter(self, name: str) -> Adapter:
        cls = self.adapter_class(name)
        config = thaw(self._table(name))
        config.pop("implementation", None)
        return cls(config)
