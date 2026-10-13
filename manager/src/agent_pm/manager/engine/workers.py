"""Worker session creation, records, early supervision, and restart."""

import contextlib
import json
import os
import stat
import sys
import time
import uuid
from typing import TypeGuard

from agent_pm.manager.engine import roster
from agent_pm.manager.engine.crew_context import (
    NAME,
    check_name,
    configure,
    driver_errors,
    leased,
    placed,
    session,
)
from agent_pm.manager.engine.types import (
    Entry,
    NotAWorker,
    RosterError,
    Spec,
    WorkersError,
)
from agent_pm.session import (
    Access,
    Blocked,
    DriverError,
    Exited,
    Handle,
    IdleTimeout,
    StartOptions,
)
from agent_pm.session import Spec as SessionSpec
from agent_pm.utils import printable_abs, save

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
        raise WorkersError.override_syntax(item)
    if not roster.printable(value):
        raise WorkersError.override_text(key, value)
    return key, value


def note_text(text: str) -> str | None:
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
                Entry(
                    "worker",
                    sid=record["sid"],
                    cwd=record["cwd"],
                    resume=resume,
                    note=note,
                    split=split,
                    split_from=split_from,
                )
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
            options=StartOptions(resume=worker.resume, idle_timeout=EARLY, events=record["events"]),
        )
    path = os.path.join(spec.workdir, RECORD)
    try:
        save(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        with contextlib.suppress(DriverError):
            driver.close(name)
        raise WorkersError.record_failed(path, e) from e
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
        raise WorkersError.resume_sid(spec.resume)
    if spec.note is not None:
        note_text(spec.note)
    env = dict(_override(item) for item in spec.overrides)
    with leased(directory, own, write=False, driver=driver):
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
