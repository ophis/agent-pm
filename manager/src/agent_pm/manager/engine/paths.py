"""Manager and worker directory resolution, ownership, permissions, and runtime configuration."""

import os
import stat
from typing import overload

from agent_pm.manager.engine.roster_schema import NAME, SID, matches
from agent_pm.manager.engine.types import RosterError
from agent_pm.session import Driver, DriverError
from agent_pm.utils import LOCAL, STATE, read_project

ROOT = f"{STATE}/managers"


WORKERS = f"{STATE}/workers"


MANAGER_DIR = "manager directory"


WORKER_DIR = "worker directory"


@overload
def directory(manager: str, *, driver=None) -> str: ...


@overload
def directory(manager: None = None, *, driver=None) -> str | None: ...


def directory(manager: str | None = None, *, driver=None) -> str | None:
    driver = _configure() if driver is None else driver
    if manager is None:
        try:
            manager = next((row.name for row in driver.sessions() if row.own), None)
        except DriverError as e:
            raise RosterError.caller_unknown(e) from e
        if manager is None:
            return None
    elif not matches(NAME, manager):
        raise RosterError.manager_name(manager)
    return os.path.join(os.path.abspath(os.path.expanduser(ROOT)), manager)


def checked(path: str, *, private: bool = False, what: str = MANAGER_DIR) -> None:
    try:
        st = os.lstat(path)
    except OSError as e:
        raise RosterError.directory_io(what, path, e) from e
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise RosterError.directory_owner(what, path)
    if private and st.st_mode & 0o077:
        mode = stat.S_IMODE(st.st_mode)
        raise RosterError.directory_permissions(what, path, mode)


def _made(levels: tuple[str, ...], private: tuple[str, ...], what: str) -> None:
    home = os.path.dirname(levels[0])
    try:
        os.makedirs(home, mode=0o700, exist_ok=True)
    except OSError as e:
        raise RosterError.directory_io(what, home, e) from e
    # one level at a time, 0700 whatever the umask, each checked: nothing is created through a bad
    # level
    for path in levels:
        try:
            try:
                os.mkdir(path, 0o700)
                os.chmod(path, 0o700)
            except FileExistsError:
                pass
        except OSError as e:
            raise RosterError.directory_io(what, path, e) from e
        checked(path, private=path in private, what=what)


def ensure(directory: str) -> None:
    _made((os.path.dirname(directory), directory), (directory,), MANAGER_DIR)


def worker_dir(sid) -> str:
    if not matches(SID, sid):
        raise RosterError.worker_sid(sid)
    workers = os.path.abspath(os.path.expanduser(WORKERS))
    path = os.path.join(workers, sid)
    _made((workers, path), (workers, path), WORKER_DIR)
    return path


def default_runtime(local: str = LOCAL) -> str:
    try:
        runtime = read_project("manager", local=local).get("runtime")
    except (OSError, ValueError) as e:
        raise RosterError.configuration(e) from e
    if not (isinstance(runtime, str) and runtime):
        raise RosterError.missing_runtime()
    return runtime


def home(name: str | None, events: str | None, *, driver=None) -> str | None:
    d = directory(name, driver=driver)
    if d is None or not os.path.isdir(d) or events is None:
        return None
    return d if os.path.realpath(events) == os.path.realpath(os.path.join(d, "events")) else None


def _configure():
    return Driver.from_config(read_project("session"))
