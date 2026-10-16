"""Whether a session is hidden: hide closed its view and no show reopened it since. One file per
session name, {sid, hidden}, keyed by the session's private terminal sid, so a later run under the
name (another sid) is not hidden; a session without one (a harness) is never recorded.
"""

import json
import os

from agent_pm.session.engine.types import TAG
from agent_pm.session.hosts.base import HostProcess
from agent_pm.utils import STATE, read_regular, save

DIR = f"{STATE}/hidden"
LIMIT = 4 << 10


def _path(name: str) -> str | None:
    return os.path.join(os.path.expanduser(DIR), f"{name}.json") if TAG.fullmatch(name) else None


def hidden(process: HostProcess) -> bool:
    sid, path = process.session_identity()[0], _path(process.name)
    if sid is None or path is None:
        return False
    data = read_regular(path, limit=LIMIT)
    try:
        found = None if data is None else json.loads(data)
    except ValueError:
        return False
    return isinstance(found, dict) and found.get("sid") == sid and found.get("hidden") is True


def record(process: HostProcess, *, hidden: bool) -> None:
    sid, path = process.session_identity()[0], _path(process.name)
    if sid is not None and path is not None:
        save(path, json.dumps({"sid": sid, "hidden": hidden}))
