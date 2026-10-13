"""Session discovery, worker placement, and manager lease context."""

import contextlib
import re
from collections.abc import Iterator

from agent_pm.manager.engine import roster
from agent_pm.manager.engine.types import (
    Entry,
    RosterError,
    WorkersError,
)
from agent_pm.session import (
    ConfigError,
    Driver,
    DriverError,
    SessionRow,
)
from agent_pm.utils import read_project

NAME = re.compile(r"[A-Za-z0-9_-]+")


@contextlib.contextmanager
def driver_errors() -> Iterator[None]:
    try:
        yield
    except (ConfigError, DriverError) as e:
        raise WorkersError(str(e)) from e


def check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        raise WorkersError.name(name)


def sessions(match: str | None = None, *, driver=None) -> list[SessionRow]:
    driver = configure() if driver is None else driver
    with driver_errors():
        return driver.sessions(match)


def session(name: str, *, driver=None) -> SessionRow | None:
    return next(iter(sessions(re.escape(name), driver=driver)), None)


def live_sessions(*, driver=None) -> dict[str, SessionRow]:
    return {row.name: row for row in sessions(driver=driver)}


def caller(*, driver=None) -> SessionRow | None:
    return next((row for row in sessions(driver=driver) if row.own), None)


def own_session(*, driver=None) -> str | None:
    row = caller(driver=driver)
    return None if row is None else row.name


def placed(e: dict, row: SessionRow) -> dict:
    for key in ("pane", "opener"):
        if getattr(row, key) is not None:
            with contextlib.suppress(RosterError):
                e = roster.entry(Entry(**{**e, key: getattr(row, key)}))
    return e


def place(r: dict, name: str, row: SessionRow) -> None:
    r["entries"][name] = placed(r["entries"][name], row)


def entry_sessions(name: str, e: dict, live: dict) -> tuple[str | None, str | None]:
    pane, driver = (name, None) if e["kind"] == "worker" else (e["tui"], name)
    return (pane if pane in live else None), (driver if driver in live else None)


@contextlib.contextmanager
def leased(directory: str, own: str | None, *, write: bool = True, driver=None):
    try:
        with roster.locked(directory, write=write) as r:
            roster.check(r, directory, own, driver=driver)
            yield r
    except RosterError as e:
        raise WorkersError(str(e)) from e


def manager_directory(name: str | None, *, driver=None) -> str:
    try:
        directory = roster.directory(name, driver=driver)
    except RosterError as e:
        raise WorkersError(str(e)) from e
    if directory is None:
        raise WorkersError.no_directory()
    return directory


def configure() -> Driver:
    try:
        return Driver.from_config(read_project("session"))
    except (OSError, ValueError, ConfigError) as e:
        raise WorkersError.configuration(e) from None
