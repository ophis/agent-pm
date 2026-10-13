"""parse(table): an already-read [hosts] table as Hosts(default, tables), the [hosts] parser's only
home (caller: Session.parse_hosts); else ValueError. An implementation is checked only for the
module.path:Class form, never imported.
"""

import re
from collections.abc import Mapping

from agent_pm.session.hosts.types import Hosts
from agent_pm.utils import CLASS_PATH

NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


def parse(table) -> Hosts:
    if not isinstance(table, Mapping):
        raise ValueError("[hosts]: missing")
    tables = {}
    for key, value in table.items():
        if key == "default":
            continue
        if not isinstance(key, str):
            raise ValueError(f"[hosts] {key!r}: want a string key")
        if not isinstance(value, Mapping):
            raise ValueError(f"[hosts] {key}: want default or a [hosts.<name>] table")
        if not NAME.match(key):
            raise ValueError(f"[hosts.{key}]: want a name matching [a-z][a-z0-9_]*")
        implementation = value.get("implementation")
        if not isinstance(implementation, str) or not CLASS_PATH.fullmatch(implementation):
            raise ValueError(f"[hosts.{key}].implementation: want module.path:Class")
        tables[key] = dict(value)
    default = table.get("default")
    if not isinstance(default, str) or default not in tables:
        raise ValueError(f"[hosts] default: want one of {', '.join(sorted(tables))}")
    return Hosts(default, tables)
