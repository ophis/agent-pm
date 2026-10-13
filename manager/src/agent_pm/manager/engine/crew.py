"""A manager's workers, each a plain tui session reached only through agent_pm.session, and its
roster (roster.py). start picks the runtime (--client, else the caller's own tui session's, else the
config's `runtime`, roster.default_runtime), starts the worker with its workdir
~/.agent-pm/workers/<sid>/, records it there in worker.json (what restart replays), shows it, enters
it in the roster and watches it EARLY s for a death or a dialog. attach syncs the roster with the
live sessions, reopens the unshown and prints the table, the backlog and how to arm a monitor. Exits
1 on an error, 2 on a usage error. Follow-ups go through the Driver's tmux_host.py send. Run as `env
PYTHONPATH=<the import roots>[:U] python3 -P -m agent_pm.manager.engine.crew …`, the form of each
crew command it prints. main first sets PYTHONPATH to U, the caller's minus each entry resolving to
one (unset when empty): what workers and recovered role or pipeline entries get; then configures the
session from the session config before a subcommand of STARTS (those that start, show or attach a
session); next-event configures just before it attaches, a config error becoming its content text.
The import roots (agent_pm.delegate's IMPORT_ROOTS), then U, appear only in a command running crew:
each one it prints (U as at print time) and a recovered worker entry.
"""

import argparse
import contextlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from typing import TypeGuard

from agent_pm.delegate import IMPORT_ROOTS, PYTHONPATH
from agent_pm.manager.engine import roster
from agent_pm.manager.engine.types import (
    ROTATE_MSG,
    UNPRINTABLE,
    CommandArgumentError,
    NotAWorker,
    Refused,
    RosterError,
    Spec,
    Stale,
    WorkersError,
)
from agent_pm.session import (
    Access,
    Blocked,
    ConfigError,
    Driver,
    DriverError,
    Exited,
    Handle,
    IdleTimeout,
    SessionRow,
)
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import printable_abs, read_project, save

NAME = re.compile(r"[A-Za-z0-9_-]+")


@contextlib.contextmanager
def driver_errors() -> Iterator[None]:
    try:
        yield
    except (ConfigError, DriverError) as e:
        raise WorkersError(str(e)) from e


def check_name(name: str) -> None:
    if not NAME.fullmatch(name):
        raise WorkersError(f"bad name {name!r}: use [A-Za-z0-9_-]+")


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
                e = roster.entry(**{**e, key: getattr(row, key)})
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
        raise WorkersError("no manager directory: run inside tmux or give --manager <name>")
    return directory


def configure() -> Driver:
    try:
        return Driver.from_config(read_project("session"))
    except (OSError, ValueError, ConfigError) as e:
        raise WorkersError(f"config: {e}") from None


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
                    raise WorkersError(f"events file {events}: replaced")
                if st.st_size < offset:
                    raise WorkersError(f"events file {events}: shrank")
                f.seek(offset)
                data = f.read()
        except FileNotFoundError:
            data = b""
        except OSError as e:
            raise WorkersError(f"events file {events}: {e.strerror or e}") from e
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
        raise WorkersError(f"{name}: no tmux session")
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


EARLY = 5


REPORT_LINES = 20


RECORD = "worker.json"


RECORD_MAX = 64 << 10


FIELDS = (
    "version",
    "name",
    "sid",
    "runtime",
    "cwd",
    "flags",
    "env",
    "events",
    "manager",
    "split",
    "split_from",
)


def _text(value) -> bool:
    return isinstance(value, str) and value != "" and roster.printable(value)


def _override(item: str) -> tuple[str, str]:
    key, eq, value = item.partition("=")
    if not (eq and key):
        raise WorkersError(f"--env {item}: want NAME=VALUE")
    if not roster.printable(value):
        raise WorkersError(f"--env {key}: {value!r}: {UNPRINTABLE}")
    return key, value


def note_text(text: str) -> str | None:
    if len(text) > roster.NOTE_MAX:
        error_length = len(text)
        error_maximum = roster.NOTE_MAX
        raise WorkersError(f"note: {error_length} characters: want at most {error_maximum}")
    if not roster.printable(text):
        raise WorkersError(f"note: {text!r}: {UNPRINTABLE}")
    return text or None


def _runtime(client: str | None, own_runtime: str | None) -> str:
    if client is not None:
        return client
    if own_runtime is not None:
        return own_runtime
    try:
        return roster.default_runtime()
    except RosterError as e:
        raise WorkersError(str(e)) from e


def _worker_dir(sid: str) -> str:
    try:
        return roster.worker_dir(sid)
    except RosterError as e:
        raise WorkersError(str(e)) from e


def _spec(record: dict, workdir: str) -> SessionSpec:
    protect = (roster.ROOT, roster.WORKERS, roster.CREW)
    with driver_errors():
        return SessionSpec(
            workdir=workdir,
            sid=record["sid"],
            name=record["name"],
            access=Access(cwd=record["cwd"], project=True),
            flags=tuple(record["flags"]),
            env=record["env"],
            protect=tuple(os.path.realpath(os.path.expanduser(p)) for p in protect),
        )


def _enroll(record: dict, own: str | None, note: str | None, *, driver=None) -> None:
    name, directory, split, split_from = (
        record[k] for k in ("name", "manager", "split", "split_from")
    )
    resume = [
        sys.executable,
        *roster.CREW_MODULE,
        "start",
        name,
        "--manager",
        os.path.basename(directory),
        "--client",
        record["runtime"],
        "--resume",
        record["sid"],
        "--cwd",
        record["cwd"],
        *(w for k, v in record["env"].items() for w in ("--env", f"{k}={v}")),
        *(["--split", split] if split is not None else []),
        *(["--split-from", split_from] if split_from is not None else []),
        *(["--", *record["flags"]] if record["flags"] else []),
    ]
    try:
        row = session(name, driver=driver)
    except WorkersError:
        row = None
    try:
        with leased(directory, own, driver=driver) as r:
            value = roster.entry(
                "worker",
                sid=record["sid"],
                cwd=record["cwd"],
                resume=resume,
                note=note,
                split=split,
                split_from=split_from,
            )
            roster.record(r, name, value if row is None else placed(value, row))
    except WorkersError as e:
        roster.unwritten(name, e)


def _died(name: str, runtime: str, code: int, *, driver=None) -> str:
    row = session(name, driver=driver)
    if row is None:
        return f"{name}: session ended at once"
    lines = [x for x in (row.last_lines or "").splitlines() if x.strip()]
    return f"{name}: {runtime} exited {code} at once; its pane's last lines:\n" + "\n".join(
        lines[-REPORT_LINES:]
    )


def _early(name: str, runtime: str, handle: Handle, *, driver=None) -> None:
    deadline = time.monotonic() + EARLY
    with driver_errors():
        for event in handle.events():
            if isinstance(event, Exited):
                raise WorkersError(_died(name, runtime, event.returncode, driver=driver))
            if isinstance(event, Blocked):
                print(f"workers: {name}: waits on a dialog: {event.reason}", file=sys.stderr)
                return
            if isinstance(event, IdleTimeout) or time.monotonic() >= deadline:
                return


def _launch(
    record: dict,
    spec: SessionSpec,
    own: str | None,
    worker: Spec,
    *,
    driver=None,
) -> None:
    driver = configure() if driver is None else driver
    name = record["name"]
    with driver_errors():
        handle = driver.start(
            record["runtime"],
            "tui",
            worker.prompt or "",
            spec,
            resume=worker.resume,
            idle_timeout=EARLY,
            events=record["events"],
        )
    path = os.path.join(spec.workdir, RECORD)
    try:
        save(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        with contextlib.suppress(DriverError):
            driver.close(name)
        raise WorkersError(f"{path}: {e.strerror or e}; start undone") from e
    print(f"tui: session {name}: {driver.attach_command(name)}", file=sys.stderr)
    with driver_errors():
        driver.show(name, split=record["split"], split_from=record["split_from"])
    _enroll(record, own, None if worker.note is None else note_text(worker.note), driver=driver)
    _early(name, record["runtime"], handle, driver=driver)


def start(
    spec: Spec,
    *,
    directory: str,
    own: str | None,
    own_runtime: str | None = None,
    driver=None,
) -> str:
    check_name(spec.name)
    if spec.split_from is not None:
        check_name(spec.split_from)
    if spec.resume is not None and not roster.SID.fullmatch(spec.resume):
        raise WorkersError(f"--resume {spec.resume}: not a session id")
    if spec.note is not None:
        note_text(spec.note)
    env = dict(_override(item) for item in spec.overrides)
    with leased(directory, own, write=False, driver=driver):
        pass
    cwd = os.path.abspath(spec.cwd)
    if not os.path.isdir(cwd):
        raise WorkersError(f"--cwd {cwd}: not a directory")
    record = {
        "version": 1,
        "name": spec.name,
        "sid": spec.resume or str(uuid.uuid4()),
        "runtime": _runtime(spec.client, own_runtime),
        "cwd": cwd,
        "flags": list(spec.flags),
        "env": env,
        "events": roster.events(directory, create=False),
        "manager": os.path.abspath(directory),
        "split": spec.split,
        "split_from": spec.split_from,
    }
    workdir = _worker_dir(record["sid"])
    _launch(
        record,
        _spec(record, workdir),
        own,
        spec,
        driver=driver,
    )
    return record["sid"]


def _worker(data: object, name: str, sid: str) -> TypeGuard[dict]:
    if not (isinstance(data, dict) and set(data) == set(FIELDS)):
        return False
    flags, env, manager, split_from = (data[k] for k in ("flags", "env", "manager", "split_from"))
    return (
        type(data["version"]) is int
        and data["version"] == 1
        and (data["name"], data["sid"]) == (name, sid)
        and _text(data["runtime"])
        and all(printable_abs(data[k]) for k in ("cwd", "events", "manager"))
        and NAME.fullmatch(os.path.basename(manager)) is not None
        and isinstance(flags, list)
        and all(isinstance(f, str) and "\0" not in f for f in flags)
        and isinstance(env, dict)
        and all(
            k and "=" not in k and isinstance(v, str) and roster.printable(v)
            for k, v in env.items()
        )
        and data["split"] in (None, *roster.SPLITS)
        and (
            split_from is None
            or isinstance(split_from, str)
            and NAME.fullmatch(split_from) is not None
        )
    )


def _recorded(workdir: str, name: str, sid: str) -> dict:
    """The worker record, checked as start checks its options; anything else: NotAWorker."""
    try:
        fd = os.open(os.path.join(workdir, RECORD), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise NotAWorker(name) from None
    try:
        st = os.fstat(fd)
        raw = b""
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid():
            with os.fdopen(fd, "rb", closefd=False) as f:
                raw = f.read(RECORD_MAX + 1)
        data = json.loads(raw) if len(raw) <= RECORD_MAX else None
    except (OSError, ValueError, RecursionError):
        data = None
    finally:
        os.close(fd)
    if not _worker(data, name, sid):
        raise NotAWorker(name)
    return data


def restart(name: str, *, own: str | None, driver=None) -> None:
    driver = configure() if driver is None else driver
    check_name(name)
    row = session(name, driver=driver)
    if row is None or row.sid is None or not roster.SID.fullmatch(row.sid):
        raise NotAWorker(name)
    workdir = _worker_dir(row.sid)
    record = _recorded(workdir, name, row.sid)
    spec = _spec(record, workdir)
    with driver_errors():
        driver.close(name)
    _launch(record, spec, own, Spec(name, record["cwd"], resume=row.sid), driver=driver)


def reply(name: str, *, driver=None) -> str | None:
    driver = configure() if driver is None else driver
    check_name(name)
    with driver_errors():
        return driver.attach(name).reply


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
            raise WorkersError(f"{name} not in roster")
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
        raise WorkersError("stop-all: not in tmux: run it in the manager's tmux session")
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
            raise WorkersError(f"{name}: session {live_session} is live")


STARTS = ("start", "restart", "attach", "reply")


def _after(value: str) -> int | None:
    if value == "end":
        return None
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise CommandArgumentError(f"{value!r}: use a line number or end")


def _number(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise CommandArgumentError(f"{value!r}: use a non-negative integer")


def _drop_import_root() -> None:
    roots = {os.path.realpath(r) for r in IMPORT_ROOTS}
    entries = os.environ.pop("PYTHONPATH", "").split(os.pathsep)
    if user := os.pathsep.join(p for p in entries if os.path.realpath(p) not in roots):
        os.environ["PYTHONPATH"] = user


def _start_arguments(sub) -> None:
    p = sub.add_parser("start")
    p.add_argument("name")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p.add_argument("--cwd")
    p.add_argument("--prompt")
    p.add_argument("--resume", metavar="SID", help="a session id")
    p.add_argument(
        "--client",
        metavar="NAME",
        help="the worker's runtime (default: your tui session's, else the config's runtime)",
    )
    p.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="sets a runtime-declared default in the worker's env; repeatable",
    )
    p.add_argument("--note", help="its roster note, as crew.py note sets it")
    p.add_argument("--split-from")
    p.add_argument("--split", choices=("right", "below"))


def _monitor_arguments(sub) -> None:
    p = sub.add_parser("next-event")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p.add_argument("--after", type=_after, required=True)
    p.add_argument("--gen", type=_number, required=True)
    p = sub.add_parser("attach")
    p.add_argument(
        "--manager", help="manager directory to attach to (default: your tmux session's)"
    )
    p.add_argument(
        "--after",
        type=_number,
        metavar="LINE",
        help="the line of the last event handled; with --gen",
    )
    p.add_argument(
        "--gen", type=_number, metavar="G", help="the generation of that line; with --after"
    )
    p.add_argument(
        "--resume", action="store_true", help="then run each gone entry's recovery command"
    )


def _roster_arguments(sub) -> None:
    p = sub.add_parser("release")
    p.add_argument("--manager", help="manager directory to release (default: your tmux session's)")
    p = sub.add_parser("note")
    p.add_argument("name")
    p.add_argument("text", help="'' clears it")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p = sub.add_parser("stop")
    p.add_argument("name", help="a roster entry, or its tui session")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")
    p = sub.add_parser(
        "stop-all", help="stop every session your tmux session opened, and those they opened"
    )
    p.add_argument("--dry-run", action="store_true", help="print them, stop nothing")
    p = sub.add_parser("forget")
    p.add_argument("name")
    p.add_argument("--manager", help="manager directory to use (default: your tmux session's)")


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="crew.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    _start_arguments(sub)
    _monitor_arguments(sub)
    _roster_arguments(sub)
    for cmd in ("restart", "reply"):
        sub.add_parser(cmd).add_argument("name")
    return ap


def _attach(a, driver) -> int:
    text, entries = attach(
        manager_directory(a.manager, driver=driver),
        own_session(driver=driver),
        after=a.after,
        gen=a.gen,
        driver=driver,
    )
    print(text, end="", flush=True)
    failed = False
    if a.resume:
        for ok, line in recover(entries):
            print(line, flush=True)
            failed = failed or not ok
    return int(failed)


def _stop_all(a, driver) -> None:
    lines, left = stop_all(own_session(driver=driver), dry_run=a.dry_run, driver=driver)
    for line in lines or ([] if left else ["workers: stop-all: nothing to stop"]):
        print(line)
    if left:
        raise WorkersError(f"stop-all: still live after tmux kill-session: {', '.join(left)}")


def _roster_command(a, driver) -> int:
    if a.cmd == "attach":
        return _attach(a, driver)
    if a.cmd == "stop-all":
        _stop_all(a, driver)
        return 0
    directory = manager_directory(a.manager, driver=driver)
    own = own_session(driver=driver)
    if a.cmd == "release":
        release(directory, own, driver=driver)
        print(f"workers: released {directory}")
    elif a.cmd == "note":
        note(directory, own, a.name, a.text, driver=driver)
    elif a.cmd == "stop":
        stop(directory, own, a.name, driver=driver)
    else:
        forget(directory, own, a.name, driver=driver)
    return 0


def _worker_command(a, flags, driver) -> None:
    if a.cmd == "start":
        directory = manager_directory(a.manager, driver=driver)
        parent = caller(driver=driver)
        spec = Spec(
            a.name,
            a.cwd or os.getcwd(),
            prompt=a.prompt,
            note=a.note,
            flags=tuple(flags),
            client=a.client,
            overrides=tuple(a.env),
            resume=a.resume,
            split=a.split,
            split_from=a.split_from,
        )
        sid = start(
            spec,
            directory=directory,
            own=None if parent is None else parent.name,
            own_runtime=None if parent is None else parent.runtime,
            driver=driver,
        )
        print(f"{a.name} {sid}")
    elif a.cmd == "restart":
        restart(a.name, own=own_session(driver=driver), driver=driver)
    else:
        text = reply(a.name, driver=driver)
        if text:
            print(text)


def _next_event(a, driver, event_source) -> None:
    events, after, due = record_cursor(
        manager_directory(a.manager, driver=driver),
        own_session(driver=driver),
        a.after,
        a.gen,
        driver=driver,
    )
    if due:
        raise Refused(ROTATE_MSG)
    read_event = next_event if event_source is None else event_source.next
    line, event = read_event(events, after)
    print(f"{line} {event}")
    try:
        text = content(event, driver=driver)
    except WorkersError as e:
        text = f"workers: {e}"  # stdout: background readers watch only it
    if text:
        print(text)


def main(argv=None, *, driver=None, event_source=None) -> int:
    _drop_import_root()
    argv = sys.argv[1:] if argv is None else list(argv)
    flags = []
    if "--" in argv:
        i = argv.index("--")
        argv, flags = argv[:i], argv[i + 1 :]
    ap = _parser()
    a = ap.parse_args(argv)
    if a.cmd == "attach" and (a.after is None) != (a.gen is None):
        ap.error("--after and --gen go together")
    try:
        if a.cmd in STARTS:
            driver = configure() if driver is None else driver
        if a.cmd in ("start", "restart", "reply"):
            _worker_command(a, flags, driver)
        elif a.cmd == "next-event":
            _next_event(a, driver, event_source)
        else:
            return _roster_command(a, driver)
    except Refused as e:
        print(f"workers: {e}")
        return 1
    except WorkersError as e:
        print(f"workers: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
