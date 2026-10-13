"""roster.json, the roster in a manager's directory ~/.agent-pm/managers/<name>/, beside its
`events` file; its format: TOP, FIELDS, _CHECKS. An entry is written by the script that starts the
subordinate, at start, replacing one of the same name (record): crew.py start (a worker, under its
name), the old core's drive.detach for drive.py --detach (a role) and router.py --tui (a pipeline),
keyed by the driver session (the new core's: TASK-315). `crew.py stop` removes one with its sessions
(stop), `crew.py forget` one whose sessions are gone. cursor: the number of the last `events` line
handled (0: none); gen: the generation of `events`, + 1 per rotation (rotate): a line number means
something only with its gen. Sessions are reached only through agent_pm.session (sessions, close).
worker_dir: a worker's dir ~/.agent-pm/workers/<sid>/; default_runtime: `runtime` as
read_project("manager") reads it (delegate/config.toml's, config.local.toml on top), which recovery
gives a worker entry without --client. A worker entry runs crew as a module (CREW_MODULE; its
PYTHONPATH: crew.py); recovery rewrites an entry naming a crew.py or workers.py script, or the
module agent_pm.crew, to that form.
"""

import contextlib
import datetime
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict

from agent_pm.manager.engine.paths import ROOT as ROOT
from agent_pm.manager.engine.paths import WORKERS as WORKERS
from agent_pm.manager.engine.paths import checked
from agent_pm.manager.engine.paths import default_runtime as default_runtime
from agent_pm.manager.engine.paths import directory as directory
from agent_pm.manager.engine.paths import ensure as ensure
from agent_pm.manager.engine.paths import home as home
from agent_pm.manager.engine.paths import worker_dir as worker_dir
from agent_pm.manager.engine.roster_schema import (
    CREW_MODULE,
    LEGACY,
    LEGACY_MODULE,
    VERSION,
    entry_fault,
    validated,
    value_fault,
)
from agent_pm.manager.engine.roster_schema import ID_MAX as ID_MAX
from agent_pm.manager.engine.roster_schema import KINDS as KINDS
from agent_pm.manager.engine.roster_schema import NAME as NAME
from agent_pm.manager.engine.roster_schema import NOTE_MAX as NOTE_MAX
from agent_pm.manager.engine.roster_schema import SID as SID
from agent_pm.manager.engine.roster_schema import SPLITS as SPLITS
from agent_pm.manager.engine.roster_schema import STATES as STATES
from agent_pm.manager.engine.roster_schema import printable as printable
from agent_pm.manager.engine.types import Entry, Held, RosterError, Stale
from agent_pm.session import Driver, DriverError
from agent_pm.utils import read_project

CREW = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "crew.py"))
ROSTER_MAX = 1 << 20
EVENTS_MAX = 1 << 20
SETTLE = 1
BLOCK = 1 << 16
LOCK_TIMEOUT = 30
POLL = 0.1
CONTINUE = "Continue the unfinished task."


def events(directory: str, *, create: bool = True) -> str:
    directory = os.path.abspath(directory)
    path = os.path.join(directory, "events")
    if create:
        ensure(directory)
        with _writing(path, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK):
            pass
    return path


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _owned_file(fd: int, path: str) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise RosterError.file_owner(path)


def _open(path: str, flags: int) -> int | None:
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    except FileNotFoundError as e:
        if flags & os.O_CREAT:
            raise RosterError.file_io(path, e) from e
        return None
    except OSError as e:
        raise RosterError.file_io(path, e) from e
    try:
        _owned_file(fd, path)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _locked(path: str) -> Iterator[None]:
    fd = _open(path, os.O_RDWR | os.O_CREAT)
    assert fd is not None
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RosterError.busy(path) from None
                time.sleep(POLL)
            except OSError as e:
                raise RosterError.file_io(path, e) from e
        yield
    finally:
        os.close(fd)


def _read(path: str):
    fd = _open(path, os.O_RDONLY | os.O_NONBLOCK)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as f:
        raw = f.read(ROSTER_MAX + 1)
    if len(raw) > ROSTER_MAX:
        raise RosterError.too_large(path, ROSTER_MAX)
    try:
        return json.loads(raw)
    except RecursionError:
        raise RosterError.invalid_json(path) from None
    except ValueError as e:
        raise RosterError.invalid_json(path, e) from e


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _write(directory: str, path: str, text: str) -> None:
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix="roster.json.")
    except OSError as e:
        raise RosterError.file_io(path, e) from e
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise RosterError.file_io(path, e) from e


@contextlib.contextmanager
def locked(directory: str, *, write: bool = True) -> Iterator[dict]:
    # not reentrant: never call locked() inside a block (a second flock waits for the first)
    directory = os.path.abspath(directory)
    if not os.path.lexists(directory):
        raise RosterError.not_attached(directory)
    checked(os.path.dirname(directory))
    checked(directory, private=True)
    path = os.path.join(directory, "roster.json")
    with _locked(os.path.join(directory, "roster.lock")):
        data = _read(path)
        missing = data is None
        r = validated(
            {"version": VERSION, "holder": None, "cursor": 0, "gen": 0, "entries": {}}
            if missing
            else data,
            path,
            strict=False,
        )
        before = _dump(r)
        yield r
        if write:
            validated(r, path, strict=True)
            text = _dump(r)
            if missing or text != before:
                _write(directory, path, text)


def _live(session: str, *, driver=None) -> bool:
    driver = _configure() if driver is None else driver
    try:
        return bool(driver.sessions(re.escape(session)))
    except DriverError:
        return False


def _holds(r: dict, directory: str, own: str | None, *, driver=None) -> bool:
    holder = r["holder"]
    if holder is None:
        return False
    if holder["session"] == own:
        return True
    if _live(holder["session"], driver=driver):
        raise Held(directory, holder["session"], holder["since"])
    return False


def take(r: dict, directory: str, own: str | None, *, driver=None) -> None:
    if not _holds(r, directory, own, driver=driver) and own is not None:
        r["holder"] = {"session": own, "since": now()}


def check(r: dict, directory: str, own: str | None, *, driver=None) -> None:
    # the lease holds only for this block: a take-over can follow, so a later write must not assume
    # it
    if not _holds(r, directory, own, driver=driver) and own is not None:
        raise RosterError.not_attached(directory)


def entry(spec: Entry) -> dict:
    value = asdict(spec)
    if spec.started is None:
        value["started"] = now()
    if bad := value_fault(value):
        raise RosterError.invalid_entry(bad)
    return value


def set_entry(r: dict, name: str, value: dict) -> None:
    if bad := entry_fault(name, value):
        raise RosterError.invalid_entry(bad, name)
    r["entries"][name] = value


def record(r: dict, name: str, value: dict) -> None:
    old = r["entries"].get(name)
    set_entry(r, name, value)
    if (
        value["note"] is None
        and old is not None
        and old["sid"] is not None
        and old["sid"] == value["sid"]
    ):
        r["entries"][name] = {**value, "note": old["note"]}


def unwritten(name: str, error: Exception) -> None:
    print(f"manager: entry {name} not written: {error}", file=sys.stderr)


def remove(r: dict, name: str) -> dict:
    try:
        return r["entries"].pop(name)
    except KeyError:
        raise RosterError.missing_entry(name) from None


def stop(r: dict, name: str, *, driver=None) -> str:
    driver = _configure() if driver is None else driver
    entries = r["entries"]
    key = (
        name
        if name in entries
        else next((k for k in sorted(entries) if entries[k]["tui"] == name), None)
    )
    if key is None:
        raise RosterError.missing_entry(name)
    for session in (key, entries[key]["tui"]):
        if session is None:
            continue
        with contextlib.suppress(DriverError):
            driver.close(session)
        if _live(session, driver=driver):
            raise RosterError.kill_failed(key, session)
    remove(r, key)
    return key


def recovery(entry: dict) -> list[str] | None:
    resume = list(entry["resume"])
    if os.path.basename(resume[1]) in ("crew.py", LEGACY):
        resume[1:2] = CREW_MODULE
    elif tuple(resume[1:4]) == LEGACY_MODULE:
        resume[1:4] = CREW_MODULE
    options = resume[: resume.index("--")] if "--" in resume else resume
    start = 1 + (len(CREW_MODULE) if tuple(resume[1:4]) == CREW_MODULE else 1)
    if entry["kind"] == "worker" and len(resume) > start + 1 and "--client" not in options:
        resume[start + 2 : start + 2] = ["--client", default_runtime()]
    if entry["kind"] != "role":
        return resume
    if entry["sid"] is None:
        return None
    return [*resume, "--sid", entry["sid"], "--resume", "--input", CONTINUE]


@contextlib.contextmanager
def _reading(path: str) -> Iterator:
    # O_NONBLOCK: a FIFO never blocks
    fd = _open(path, os.O_RDONLY | os.O_NONBLOCK)
    if fd is None:
        yield None
        return
    with os.fdopen(fd, "rb") as f:
        try:
            yield f
        except OSError as e:
            raise RosterError.file_io(path, e) from e


def _offset(f, n: int) -> int:
    pos = 0
    while block := f.read(BLOCK):
        parts = block.split(b"\n", n)
        if len(parts) > n:
            return pos + len(block) - len(parts[-1])
        n -= len(parts) - 1
        pos += len(block)
    return pos


def lines(path: str) -> int:
    with _reading(path) as f:
        return (
            0
            if f is None
            else sum(block.count(b"\n") for block in iter(lambda: f.read(BLOCK), b""))
        )


def offset(path: str, n: int) -> int:
    with _reading(path) as f:
        return 0 if f is None else _offset(f, n)


def following(path: str, n: int) -> bytes:
    with _reading(path) as f:
        if f is None:
            return b""
        f.seek(_offset(f, n))
        return f.read()


def due(r: dict, directory: str) -> bool:
    # a rotation then drops at least EVENTS_MAX // 2: never one for unhandled lines alone, nor per
    # handled event
    path = events(directory, create=False)
    try:
        size = os.lstat(path).st_size
    except FileNotFoundError:
        return False
    except OSError as e:
        raise RosterError.file_io(path, e) from e
    return size > EVENTS_MAX and offset(path, r["cursor"]) >= EVENTS_MAX // 2


def advance(r: dict, directory: str, after: int | None, gen: int | None) -> int:
    if gen != r["gen"]:
        raise Stale.generation(gen, r["gen"])
    path = events(directory, create=False)
    n = lines(path)
    if after is None:
        after = n
    elif after > n:
        raise Stale.past_end(after, path, n)
    r["cursor"] = max(r["cursor"], after)
    return after


@contextlib.contextmanager
def _writing(path: str, flags: int) -> Iterator[int]:
    fd = _open(path, flags)
    if fd is None:
        raise RosterError.file_missing(path)
    try:
        yield fd
    except OSError as e:
        raise RosterError.file_io(path, e) from e
    finally:
        os.close(fd)


def _refill(path: str, old: str, cursor: int, sleep) -> None:
    with _writing(path, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK) as fd:
        os.fchmod(fd, 0o600)  # a writer's `>>` may have made it in between, with its umask
    # lets in-flight appends land in `old` before its lines are copied
    sleep(SETTLE)
    data = following(old, cursor)
    if data and not data.endswith(b"\n"):
        data += b"\n"
    with _writing(path, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK) as fd:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]


def rotate(r: dict, directory: str, *, sleep=None) -> bool:
    if not due(r, directory):
        return False
    path = events(directory, create=False)
    old = path + ".1"
    for p in (path, old):
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            continue
        except OSError as e:
            raise RosterError.file_io(p, e) from e
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise RosterError.file_owner(p)
    try:
        os.replace(path, old)
    except OSError as e:
        raise RosterError.file_io(path, e) from e
    cursor = r["cursor"]
    # never raises after the rename: cursor and gen still change, so the new file is never numbered
    # by the old
    try:
        _refill(path, old, cursor, sleep or time.sleep)
    except RosterError as e:
        print(f"manager: rotate: {e}: lines after {cursor} of {old} not copied", file=sys.stderr)
    r["cursor"], r["gen"] = 0, r["gen"] + 1
    return True


def _configure():
    return Driver.from_config(read_project("session"))
