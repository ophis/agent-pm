"""parse(table): an already-read [hosts] table as Hosts(default, tables), the [hosts] parser's only
home (callers: parse_hosts); else ValueError. An implementation is checked only for the
module.path:Class form, never imported.
"""

import re
from collections.abc import Mapping

from agent_pm.session.engine.types import ConfigError
from agent_pm.session.hosts.types import Hosts, HostsError
from agent_pm.utils import CLASS_PATH

NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


def parse(table) -> Hosts:
    if not isinstance(table, Mapping):
        raise HostsError.missing()
    tables = {}
    for key, value in table.items():
        if key == "default":
            continue
        if not isinstance(key, str):
            raise HostsError.key_type(key)
        if not isinstance(value, Mapping):
            raise HostsError.table(key)
        if not NAME.match(key):
            raise HostsError.name(key)
        implementation = value.get("implementation")
        if not isinstance(implementation, str) or not CLASS_PATH.fullmatch(implementation):
            raise HostsError.implementation(key)
        tables[key] = dict(value)
    default = table.get("default")
    if not isinstance(default, str) or default not in tables:
        raise HostsError.default(tables)
    return Hosts(default, tables)


def parse_hosts(table) -> Hosts:
    """Read host settings through the session package, reporting configuration failures."""
    try:
        return parse(table)
    except ValueError as error:
        raise ConfigError(str(error)) from None
