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
    StartOptions,
)
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import printable_abs, read_project, save

NAME = re.compile(r"[A-Za-z0-9_-]+")
EVENT = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2} \S+ \S.*")
EARLY = 5
REPORT_LINES = 20
REPLY_LINES = 80
PANE_LINES = 40
RUN_TAIL = 1 << 20
ENDED = {"done": "finished", "failed": "finished", "needs_input": "waiting"}
HEADER = "name\tkind\tsid\tstate\tnote\tcwd\tpane"
RECORD = "worker.json"
STARTS = ("start", "restart", "attach", "reply")
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


@contextlib.contextmanager
def _driver() -> Iterator[None]:
    try:
        yield
    except (ConfigError, DriverError) as e:
        raise WorkersError(str(e)) from e


def _check(name: str) -> None:
    if not NAME.fullmatch(name):
        raise WorkersError.name(name)


def _rows(match: str | None = None, *, driver=None) -> list[SessionRow]:
    driver = _configure() if driver is None else driver
    with _driver():
        return driver.sessions(match)


def _row(name: str, *, driver=None) -> SessionRow | None:
    return next(iter(_rows(re.escape(name), driver=driver)), None)


def _live(*, driver=None) -> dict[str, SessionRow]:
    return {row.name: row for row in _rows(driver=driver)}


def _caller(*, driver=None) -> SessionRow | None:
    return next((row for row in _rows(driver=driver) if row.own), None)


def _own(*, driver=None) -> str | None:
    row = _caller(driver=driver)
    return None if row is None else row.name


def _text(value) -> bool:
    return isinstance(value, str) and value != "" and roster.printable(value)


def _override(item: str) -> tuple[str, str]:
    key, eq, value = item.partition("=")
    if not (eq and key):
        raise WorkersError.override_syntax(item)
    if not roster.printable(value):
        raise WorkersError.override_text(key, value)
    return key, value


def _note_text(text: str) -> str | None:
    if len(text) > roster.NOTE_MAX:
        raise WorkersError.note_length(len(text), roster.NOTE_MAX)
    if not roster.printable(text):
        raise WorkersError.note_text(text)
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
    with _driver():
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
        row = _row(name, driver=driver)
    except WorkersError:
        row = None
    try:
        with _leased(directory, own, driver=driver) as r:
            value = roster.entry(
                "worker",
                sid=record["sid"],
                cwd=record["cwd"],
                resume=resume,
                note=note,
                split=split,
                split_from=split_from,
            )
            roster.record(r, name, value if row is None else _placed(value, row))
    except WorkersError as e:
        roster.unwritten(name, e)


def _died(name: str, runtime: str, code: int, *, driver=None) -> str:
    row = _row(name, driver=driver)
    if row is None:
        return f"{name}: session ended at once"
    lines = [x for x in (row.last_lines or "").splitlines() if x.strip()]
    return f"{name}: {runtime} exited {code} at once; its pane's last lines:\n" + "\n".join(
        lines[-REPORT_LINES:]
    )


def _early(name: str, runtime: str, handle: Handle, *, driver=None) -> None:
    deadline = time.monotonic() + EARLY
    with _driver():
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
    *,
    prompt: str,
    resume: str | None,
    note,
    driver=None,
) -> None:
    driver = _configure() if driver is None else driver
    name = record["name"]
    with _driver():
        handle = driver.start(
            record["runtime"],
            "tui",
            prompt,
            spec,
            options=StartOptions(resume=resume, idle_timeout=EARLY, events=record["events"]),
        )
    path = os.path.join(spec.workdir, RECORD)
    try:
        save(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        with contextlib.suppress(DriverError):
            driver.close(name)
        raise WorkersError.record_failed(path, e) from e
    print(f"tui: session {name}: {driver.attach_command(name)}", file=sys.stderr)
    with _driver():
        driver.show(name, split=record["split"], split_from=record["split_from"])
    _enroll(record, own, note, driver=driver)
    _early(name, record["runtime"], handle, driver=driver)


def start(
    spec: Spec,
    *,
    directory: str,
    own: str | None,
    own_runtime: str | None = None,
    split: str | None = None,
    split_from: str | None = None,
    driver=None,
) -> str:
    _check(spec.name)
    if split_from is not None:
        _check(split_from)
    if spec.resume is not None and not roster.SID.fullmatch(spec.resume):
        raise WorkersError.resume_sid(spec.resume)
    note = None if spec.note is None else _note_text(spec.note)
    env = dict(_override(item) for item in spec.overrides)
    with _leased(directory, own, write=False, driver=driver):
        pass
    cwd = os.path.abspath(spec.cwd)
    if not os.path.isdir(cwd):
        raise WorkersError.cwd(cwd)
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
        "split": split,
        "split_from": split_from,
    }
    workdir = _worker_dir(record["sid"])
    _launch(
        record,
        _spec(record, workdir),
        own,
        prompt=spec.prompt or "",
        resume=spec.resume,
        note=note,
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
    driver = _configure() if driver is None else driver
    _check(name)
    row = _row(name, driver=driver)
    if row is None or row.sid is None or not roster.SID.fullmatch(row.sid):
        raise NotAWorker(name)
    workdir = _worker_dir(row.sid)
    record = _recorded(workdir, name, row.sid)
    spec = _spec(record, workdir)
    with _driver():
        driver.close(name)
    _launch(record, spec, own, prompt="", resume=row.sid, note=None, driver=driver)


def reply(name: str, *, driver=None) -> str | None:
    driver = _configure() if driver is None else driver
    _check(name)
    with _driver():
        return driver.attach(name).reply


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
    driver = _configure() if driver is None else driver
    name, kind = event.split(" ")[1:3]
    if kind not in ("done", "blocked", "dead"):
        return ""
    if kind == "done":
        driver = _configure() if driver is None else driver
        try:
            text = driver.attach(name).reply
        except (ConfigError, DriverError):
            text = None
        if text is not None:
            return "\n".join(text.split("\n")[:REPLY_LINES])
    row = _row(name, driver=driver)
    if row is None:
        raise WorkersError.missing_session(name)
    if row.state == "dead":
        return "\n".join((row.last_lines or "").split("\n")[-PANE_LINES:])
    driver = _configure() if driver is None else driver
    with _driver():
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


def _placed(e: dict, row: SessionRow) -> dict:
    for key in ("pane", "opener"):
        if getattr(row, key) is not None:
            with contextlib.suppress(RosterError):
                e = roster.entry(**{**e, key: getattr(row, key)})
    return e


def _place(r: dict, name: str, row: SessionRow) -> None:
    r["entries"][name] = _placed(r["entries"][name], row)


def _sessions(name: str, e: dict, live: dict) -> tuple[str | None, str | None]:
    pane, driver = (name, None) if e["kind"] == "worker" else (e["tui"], name)
    return (pane if pane in live else None), (driver if driver in live else None)


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
        pane, driver = _sessions(name, e, live)
        if pane is None and driver is None:
            status = None if e["kind"] == "worker" else _status(e["cwd"])
            e["state"] = ENDED.get(status, "gone") if status is not None else "gone"
            continue
        e["state"] = live[pane].state if pane else "working"
        if pane:
            _place(r, name, live[pane])
            panes[name] = pane
    return panes


def _reopen(
    panes: dict, entries: dict, live: dict[str, SessionRow], *, driver=None
) -> tuple[dict, list]:
    driver = _configure() if driver is None else driver
    shown, opened = {}, []
    for name in sorted(panes):
        session, e = panes[name], entries[name]
        if live[session].shown is not None:
            shown[name] = live[session].shown or "a terminal"
            continue
        print(f"tui: session {session}: {driver.attach_command(session)}", file=sys.stderr)
        with _driver():
            driver.show(session, split=e["split"], split_from=e["split_from"])
        opened.append((name, e["kind"], e["sid"], session))
    return shown, opened


def _pythonpath(argv: list[str]) -> dict[str, str]:
    if tuple(argv[1:4]) != roster.CREW_MODULE:
        return {}
    user = os.environ.get("PYTHONPATH")
    return {"PYTHONPATH": os.pathsep.join(filter(None, (PYTHONPATH, user)))}


def _command(argv: list[str]) -> str:
    env = "".join(f"{k}={shlex.quote(v)} " for k, v in _pythonpath(argv).items())
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
            live = _live(driver=driver)
            roster.rotate(r, directory)
            panes = _sync(r, live)
            backlog, cursor, gen = _backlog(events, r["cursor"]), r["cursor"], r["gen"]
        shown, opened = _reopen(panes, r["entries"], live, driver=driver)
        if opened:
            live = _live(driver=driver)
            with roster.locked(directory) as r:
                for name, kind, sid, session in opened:
                    e = r["entries"].get(name)
                    if e is not None and (e["kind"], e["sid"]) == (kind, sid) and session in live:
                        _place(r, name, live[session])
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
                env={**os.environ, **_pythonpath(argv)},
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
        except Exception as err:
            yield False, f"resumed {name}: not run: {type(err).__name__}: {err}"
            continue
        last = next(
            (line.strip() for line in reversed(res.stdout.splitlines()) if line.strip()), ""
        )
        yield (
            res.returncode == 0,
            f"resumed {name}: exit {res.returncode}" + (f": {last}" if last else ""),
        )


@contextlib.contextmanager
def _leased(directory: str, own: str | None, *, write: bool = True, driver=None):
    try:
        with roster.locked(directory, write=write) as r:
            roster.check(r, directory, own, driver=driver)
            yield r
    except RosterError as e:
        raise WorkersError(str(e)) from e


def release(directory: str, own: str | None, *, driver=None) -> None:
    with _leased(directory, own, driver=driver) as r:
        r["holder"] = None


def note(directory: str, own: str | None, name: str, text: str, *, driver=None) -> None:
    _check(name)
    with _leased(directory, own, driver=driver) as r:
        if name not in r["entries"]:
            raise WorkersError.missing_entry(name)
        r["entries"][name]["note"] = _note_text(text)


def stop(directory: str, own: str | None, name: str, *, driver=None) -> None:
    _check(name)
    with _leased(directory, own, driver=driver) as r:
        roster.stop(r, name, driver=driver)


def opened(own: str, live: dict[str, SessionRow], entries=lambda session: {}) -> list[str]:
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
    driver = _configure() if driver is None else driver
    if own is None:
        raise WorkersError.no_caller()
    home = roster.directory(own, driver=driver)
    if os.path.isdir(home):
        with _leased(home, own, write=False, driver=driver):
            pass
    read: dict[str, dict] = {}

    def entries(session: str):
        if session not in read:
            read[session] = _entries(roster.directory(session, driver=driver))
        return read[session]

    chosen = opened(own, _live(driver=driver), entries)
    pipelines = {k for es in read.values() for k, e in es.items() if e["kind"] == "pipeline"}
    verb, left = "would stop", []
    if not dry_run:
        verb = "stopped"
        for name in chosen:
            with contextlib.suppress(DriverError):
                driver.close(name)
        after = _live(driver=driver)
        left = [n for n in chosen if n in after]
        gone = set(chosen) - set(left)
        for session in [s for s, es in read.items() if es]:
            d = roster.directory(session, driver=driver)
            try:
                with _leased(d, own, driver=driver) if session == own else roster.locked(d) as r:
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
    _check(name)
    with _leased(directory, own, driver=driver) as r:
        pane, session_driver = _sessions(name, roster.remove(r, name), _live(driver=driver))
        live_session = pane or session_driver
        if live_session is not None:
            raise WorkersError.live_session(name, live_session)


def _directory(name: str | None, *, driver=None) -> str:
    try:
        directory = roster.directory(name, driver=driver)
    except RosterError as e:
        raise WorkersError(str(e)) from e
    if directory is None:
        raise WorkersError.no_directory()
    return directory


def _record(
    directory: str, own: str | None, after: int | None, gen: int, *, driver=None
) -> tuple[str, int, bool]:
    with _leased(directory, own, driver=driver) as r:
        try:
            after = roster.advance(r, directory, after, gen)
        except Stale as e:
            raise Refused(str(e)) from e
        return roster.events(directory, create=False), after, roster.due(r, directory)


def _after(value: str) -> int | None:
    if value == "end":
        return None
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise CommandArgumentError.after(value)


def _number(value: str) -> int:
    if re.fullmatch(r"[0-9]+", value):
        return int(value)
    raise CommandArgumentError.number(value)


def _drop_import_root() -> None:
    roots = {os.path.realpath(r) for r in IMPORT_ROOTS}
    entries = os.environ.pop("PYTHONPATH", "").split(os.pathsep)
    if user := os.pathsep.join(p for p in entries if os.path.realpath(p) not in roots):
        os.environ["PYTHONPATH"] = user


def _configure() -> Driver:
    try:
        return Driver.from_config(read_project("session"))
    except (OSError, ValueError, ConfigError) as e:
        raise WorkersError.configuration(e) from None


class EventSource:
    """Blocking event-file reader for command-line monitoring."""

    def next(self, events, after):
        return next_event(events, after)


def main(argv=None, *, driver=None, event_source=None) -> int:
    _drop_import_root()
    argv = sys.argv[1:] if argv is None else list(argv)
    flags = []
    if "--" in argv:
        i = argv.index("--")
        argv, flags = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser(prog="crew.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
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
    for cmd in ("restart", "reply"):
        sub.add_parser(cmd).add_argument("name")
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
    a = ap.parse_args(argv)
    if a.cmd == "attach" and (a.after is None) != (a.gen is None):
        ap.error("--after and --gen go together")
    try:
        if a.cmd in STARTS:
            driver = _configure() if driver is None else driver
        if a.cmd == "attach":
            text, entries = attach(
                _directory(a.manager, driver=driver),
                _own(driver=driver),
                after=a.after,
                gen=a.gen,
                driver=driver,
            )
            print(text, end="", flush=True)
            if a.resume:
                failed = False
                for ok, line in recover(entries):
                    print(line, flush=True)
                    failed = failed or not ok
                if failed:
                    return 1
        elif a.cmd == "release":
            directory = _directory(a.manager, driver=driver)
            release(directory, _own(driver=driver), driver=driver)
            print(f"workers: released {directory}")
        elif a.cmd == "note":
            note(
                _directory(a.manager, driver=driver),
                _own(driver=driver),
                a.name,
                a.text,
                driver=driver,
            )
        elif a.cmd == "stop":
            stop(_directory(a.manager, driver=driver), _own(driver=driver), a.name, driver=driver)
        elif a.cmd == "stop-all":
            lines, left = stop_all(_own(driver=driver), dry_run=a.dry_run, driver=driver)
            for line in lines or ([] if left else ["workers: stop-all: nothing to stop"]):
                print(line)
            if left:
                raise WorkersError.kill_failed(left)
        elif a.cmd == "forget":
            forget(_directory(a.manager, driver=driver), _own(driver=driver), a.name, driver=driver)
        elif a.cmd == "start":
            directory = _directory(a.manager, driver=driver)
            caller = _caller(driver=driver)
            spec = Spec(
                a.name,
                a.cwd or os.getcwd(),
                prompt=a.prompt,
                note=a.note,
                flags=tuple(flags),
                client=a.client,
                overrides=tuple(a.env),
                resume=a.resume,
            )
            sid = start(
                spec,
                directory=directory,
                own=None if caller is None else caller.name,
                own_runtime=None if caller is None else caller.runtime,
                split=a.split,
                split_from=a.split_from,
                driver=driver,
            )
            print(f"{a.name} {sid}")
        elif a.cmd == "restart":
            restart(a.name, own=_own(driver=driver), driver=driver)
        elif a.cmd == "reply":
            text = reply(a.name, driver=driver)
            if text:
                print(text)
        else:
            events, after, due = _record(
                _directory(a.manager, driver=driver),
                _own(driver=driver),
                a.after,
                a.gen,
                driver=driver,
            )
            if due:
                raise Refused.rotation_due()
            line, event = (EventSource() if event_source is None else event_source).next(
                events, after
            )
            print(f"{line} {event}")
            try:
                text = content(event, driver=driver)
            except WorkersError as e:
                text = f"workers: {e}"  # stdout: background readers watch only it
            if text:
                print(text)
    except Refused as e:
        print(f"workers: {e}")
        return 1
    except WorkersError as e:
        print(f"workers: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
