"""Manager roster commands and recovery of stopped sessions."""

import contextlib
import os
import subprocess
import sys
from collections.abc import Iterator

from agent_pm.manager.engine import roster
from agent_pm.manager.engine.crew_context import (
    check_name,
    configure,
    entry_sessions,
    leased,
    live_sessions,
)
from agent_pm.manager.engine.monitor import worker_environment
from agent_pm.manager.engine.types import (
    RosterError,
    WorkersError,
)
from agent_pm.manager.engine.workers import note_text
from agent_pm.session import (
    DriverError,
    SessionRow,
)


def recover(entries: dict) -> Iterator[tuple[bool, str]]:
    for name in sorted(entries):
        e = entries[name]
        if e["state"] != "gone":
            continue
        try:
            argv = roster.recovery(e)
        except RosterError as err:
            raise WorkersError(str(err)) from err
        if argv is None:
            yield False, f"resumed {name}: not run: no sid"
            continue
        try:
            res = subprocess.run(
                argv,
                cwd=e["cwd"],
                env={**os.environ, **worker_environment(argv)},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        except OSError as err:
            yield False, f"resumed {name}: not run: {err.strerror or err}"
            continue
        # e.g. Popen's ValueError for an argv item (a NUL, a lone surrogate)
        except (ValueError, TypeError) as err:
            yield False, f"resumed {name}: not run: {type(err).__name__}: {err}"
            continue
        last = next(
            (line.strip() for line in reversed(res.stdout.splitlines()) if line.strip()), ""
        )
        yield (
            res.returncode == 0,
            f"resumed {name}: exit {res.returncode}" + (f": {last}" if last else ""),
        )


def release(directory: str, own: str | None, *, driver=None) -> None:
    with leased(directory, own, driver=driver) as r:
        r["holder"] = None


def note(directory: str, own: str | None, name: str, text: str, *, driver=None) -> None:
    check_name(name)
    with leased(directory, own, driver=driver) as r:
        if name not in r["entries"]:
            raise WorkersError.missing_entry(name)
        r["entries"][name]["note"] = note_text(text)


def stop(directory: str, own: str | None, name: str, *, driver=None) -> None:
    check_name(name)
    with leased(directory, own, driver=driver) as r:
        roster.stop(r, name, driver=driver)


def opened(own: str, live: dict[str, SessionRow], entries=lambda _session: {}) -> list[str]:
    depth, level, todo = {}, 0, {own}
    while todo:
        level += 1
        found = {n for n, row in live.items() if row.opener in todo}
        found.update(
            n
            for s in todo
            for k, e in entries(s).items()
            for n in (k, e["tui"])
            if n in live and live[n].opener in (None, s)
        )
        todo = found - set(depth) - {own}
        depth.update((n, level) for n in todo)
    return sorted(depth, key=lambda n: (-depth[n], n))


def _entries(directory: str) -> dict:
    if not os.path.isdir(directory):
        return {}
    try:
        with roster.locked(directory, write=False) as r:
            return r["entries"]
    except RosterError as e:
        print(f"workers: stop-all: {e}", file=sys.stderr)
        return {}


def stop_all(own: str | None, *, dry_run: bool = False, driver=None) -> tuple[list[str], list[str]]:
    driver = configure() if driver is None else driver
    if own is None:
        raise WorkersError.no_caller()
    home = roster.directory(own, driver=driver)
    if os.path.isdir(home):
        with leased(home, own, write=False, driver=driver):
            pass
    read: dict[str, dict] = {}

    def entries(session: str):
        if session not in read:
            read[session] = _entries(roster.directory(session, driver=driver))
        return read[session]

    chosen = opened(own, live_sessions(driver=driver), entries)
    pipelines = {k for es in read.values() for k, e in es.items() if e["kind"] == "pipeline"}
    verb, left = "would stop", []
    if not dry_run:
        verb = "stopped"
        for name in chosen:
            with contextlib.suppress(DriverError):
                driver.close(name)
        after = live_sessions(driver=driver)
        left = [n for n in chosen if n in after]
        gone = set(chosen) - set(left)
        for session in [s for s, es in read.items() if es]:
            d = roster.directory(session, driver=driver)
            try:
                with leased(d, own, driver=driver) if session == own else roster.locked(d) as r:
                    for k in [
                        k
                        for k, e in r["entries"].items()
                        if k in gone or e["tui"] in gone and k not in after
                    ]:
                        roster.remove(r, k)
            except (RosterError, WorkersError) as e:
                print(f"workers: stop-all: {e}", file=sys.stderr)
    lines = [
        f"{verb} {n}" + ("; a scheduler may resume it" if n in pipelines else "")
        for n in chosen
        if n not in left
    ]
    return lines, left


def forget(directory: str, own: str | None, name: str, *, driver=None) -> None:
    check_name(name)
    with leased(directory, own, driver=driver) as r:
        pane, session_driver = entry_sessions(
            name, roster.remove(r, name), live_sessions(driver=driver)
        )
        live_session = pane or session_driver
        if live_session is not None:
            raise WorkersError.live_session(name, live_session)
