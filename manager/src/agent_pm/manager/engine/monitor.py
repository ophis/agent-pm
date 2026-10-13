"""Roster synchronization, display, event monitoring, and cursor recording."""

import json
import os
import re
import shlex
import stat
import sys
import time

from agent_pm.delegate import PYTHONPATH
from agent_pm.manager.engine import roster
from agent_pm.manager.engine.crew_context import (
    configure,
    driver_errors,
    entry_sessions,
    leased,
    live_sessions,
    place,
    session,
)
from agent_pm.manager.engine.types import (
    Refused,
    RosterError,
    Stale,
    WorkersError,
)
from agent_pm.session import (
    ConfigError,
    DriverError,
    SessionRow,
)

EVENT = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2} \S+ \S.*")


REPLY_LINES = 80


PANE_LINES = 40


RUN_TAIL = 1 << 20


ENDED = {"done": "finished", "failed": "finished", "needs_input": "waiting"}


HEADER = "name\tkind\tsid\tstate\tnote\tcwd\tpane"


def next_event(events: str, after: int | None) -> tuple[int, str]:
    offset = lines = 0
    ident = None
    skip = after
    while True:
        try:
            with open(events, "rb") as f:
                st = os.fstat(f.fileno())
                if ident is None:
                    ident = (st.st_dev, st.st_ino)
                elif ident != (st.st_dev, st.st_ino):
                    raise WorkersError.events_replaced(events)
                if st.st_size < offset:
                    raise WorkersError.events_shrank(events)
                f.seek(offset)
                data = f.read()
        except FileNotFoundError:
            data = b""
        except OSError as e:
            raise WorkersError.events_io(events, e) from e
        complete = data[: data.rfind(b"\n") + 1]
        offset += len(complete)
        batch = complete.split(b"\n")[:-1]
        if skip is None:
            skip = len(batch)
        for raw in batch:
            lines += 1
            text = raw.decode("utf-8", errors="replace")
            if lines > skip and EVENT.fullmatch(text):
                return lines, text
        time.sleep(0.5)


def content(event: str, *, driver=None) -> str:
    driver = configure() if driver is None else driver
    name, kind = event.split(" ")[1:3]
    if kind not in ("done", "blocked", "dead"):
        return ""
    if kind == "done":
        driver = configure() if driver is None else driver
        try:
            text = driver.attach(name).reply
        except (ConfigError, DriverError):
            text = None
        if text is not None:
            return "\n".join(text.split("\n")[:REPLY_LINES])
    row = session(name, driver=driver)
    if row is None:
        raise WorkersError.missing_session(name)
    if row.state == "dead":
        return "\n".join((row.last_lines or "").split("\n")[-PANE_LINES:])
    driver = configure() if driver is None else driver
    with driver_errors():
        return driver.attach(name).screen(PANE_LINES)


def _status(cwd: str) -> str | None:
    try:
        fd = os.open(os.path.join(cwd, "run.jsonl"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            at = max(0, st.st_size - RUN_TAIL)
            f.seek(max(0, at - 1))
            # from the byte before the tail: its first piece is the line the tail cuts, or empty
            lines = f.read(RUN_TAIL + 1).split(b"\n")[1 if at else 0 :]
    except OSError:
        return None
    finally:
        os.close(fd)
    for raw in reversed(lines):
        try:
            line = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if isinstance(line, dict) and line.get("kind") == "result":
            outcome = line.get("outcome")
            status = outcome.get("status") if isinstance(outcome, dict) else None
            return status if isinstance(status, str) else None
    return None


def _sync(r: dict, live: dict[str, SessionRow]) -> dict[str, str]:
    entries = r["entries"]
    for name in sorted(entries):
        sid = entries[name]["sid"]
        if entries[name]["kind"] == "worker" and sid is not None and name not in live:
            to = [n for n, row in live.items() if row.sid == sid]
            if len(to) == 1 and to[0] not in entries:
                entries[to[0]] = entries.pop(name)
    panes = {}
    for name in sorted(entries):
        e = entries[name]
        pane, driver = entry_sessions(name, e, live)
        if pane is None and driver is None:
            status = None if e["kind"] == "worker" else _status(e["cwd"])
            e["state"] = ENDED.get(status, "gone") if status is not None else "gone"
            continue
        e["state"] = live[pane].state if pane else "working"
        if pane:
            place(r, name, live[pane])
            panes[name] = pane
    return panes


def _reopen(
    panes: dict, entries: dict, live: dict[str, SessionRow], *, driver=None
) -> tuple[dict, list]:
    driver = configure() if driver is None else driver
    shown, opened = {}, []
    for name in sorted(panes):
        session, e = panes[name], entries[name]
        if live[session].shown is not None:
            shown[name] = live[session].shown or "a terminal"
            continue
        print(f"tui: session {session}: {driver.attach_command(session)}", file=sys.stderr)
        with driver_errors():
            driver.show(session, split=e["split"], split_from=e["split_from"])
        opened.append((name, e["kind"], e["sid"], session))
    return shown, opened


def worker_environment(argv: list[str]) -> dict[str, str]:
    if tuple(argv[1:4]) != roster.CREW_MODULE:
        return {}
    user = os.environ.get("PYTHONPATH")
    return {"PYTHONPATH": os.pathsep.join(filter(None, (PYTHONPATH, user)))}


def _command(argv: list[str]) -> str:
    env = "".join(f"{k}={shlex.quote(v)} " for k, v in worker_environment(argv).items())
    return (f"env {env}" if env else "") + shlex.join(argv)


def _recovery(e: dict) -> str:
    argv = roster.recovery(e)
    if argv is None:
        return "none (no sid)"
    return (
        f"cd {shlex.quote(e['cwd'])} && {_command(argv)}" if e["kind"] == "role" else _command(argv)
    )


def _table(entries: dict, shown: dict) -> str:
    lines = [HEADER]
    for name in sorted(entries):
        e = entries[name]
        cells = [name, *("-" if e[k] is None else e[k] for k in HEADER.split("\t")[1:])]
        lines.append("\t".join(cells + ([f"shown in {shown[name]}"] if name in shown else [])))
    lines += [
        f"resume {name}: {_recovery(entries[name])}"
        for name in sorted(entries)
        if entries[name]["state"] == "gone"
    ]
    return "".join(f"{line}\n" for line in lines)


def _backlog(events: str, cursor: int) -> list[str]:
    texts = (
        raw.decode("utf-8", errors="replace")
        for raw in roster.following(events, cursor).split(b"\n")[:-1]
    )
    return [f"{n} {text}" for n, text in enumerate(texts, cursor + 1) if EVENT.fullmatch(text)]


def attach(
    directory: str,
    own: str | None,
    *,
    after: int | None = None,
    gen: int | None = None,
    driver=None,
) -> tuple[str, dict]:
    try:
        events = roster.events(directory)
        with roster.locked(directory) as r:
            roster.take(r, directory, own, driver=driver)
            if own is None:
                print(
                    f"workers: not in tmux: no lease on {directory}; another manager may attach",
                    file=sys.stderr,
                )
            if after is not None:
                roster.advance(r, directory, after, gen)
            # before the rotation: a raise after its rename would drop the new cursor and gen
            live = live_sessions(driver=driver)
            roster.rotate(r, directory)
            panes = _sync(r, live)
            backlog, cursor, gen = _backlog(events, r["cursor"]), r["cursor"], r["gen"]
        shown, opened = _reopen(panes, r["entries"], live, driver=driver)
        if opened:
            live = live_sessions(driver=driver)
            with roster.locked(directory) as r:
                for name, kind, sid, session in opened:
                    e = r["entries"].get(name)
                    if e is not None and (e["kind"], e["sid"]) == (kind, sid) and session in live:
                        place(r, name, live[session])
        table = _table(r["entries"], shown)
    except RosterError as e:
        raise WorkersError(str(e)) from e
    me, name = (
        _command(["python3", *roster.CREW_MODULE]),
        shlex.quote(os.path.basename(os.path.dirname(events))),
    )
    arm = [
        f"N={cursor} GEN={gen}",
        f"monitor: tail -n +{cursor + 1} -F {shlex.quote(events)}",
        f"monitor expired: {me} attach --manager {name} --after LINE --gen {gen} --resume",
        f"next-event: {me} next-event --manager {name} --after {cursor} --gen {gen}",
    ]
    return table + "".join(f"{line}\n" for line in backlog + arm), r["entries"]


def record_cursor(
    directory: str, own: str | None, after: int | None, gen: int, *, driver=None
) -> tuple[str, int, bool]:
    with leased(directory, own, driver=driver) as r:
        try:
            after = roster.advance(r, directory, after, gen)
        except Stale as e:
            raise Refused(str(e)) from e
        return roster.events(directory, create=False), after, roster.due(r, directory)
