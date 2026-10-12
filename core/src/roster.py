"""roster.json, the roster in a manager's directory ~/.agent-pm/managers/<name>/, beside its `events` file; its
format: TOP, FIELDS, _CHECKS. An entry is written by the script that starts the subordinate, at start, replacing one of
the same name (record): crew.py start (a worker, under its name), drive.detach for drive.py --detach (a role) and
router.py --tui (a pipeline), keyed by the driver session. `crew.py stop` removes one with its sessions (stop),
`crew.py forget` one whose sessions are gone. cursor: the number of the last `events` line handled (0: none); gen: the
generation of `events`, + 1 per rotation (rotate): a line number means something only with its gen."""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Iterator
from datetime import UTC, datetime

import tmux_host

ROOT = "~/.agent-pm/managers"
SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
VERSION = 1
KINDS = ("worker", "role", "pipeline")
STATES = ("working", "done", "blocked", "dead", "gone", "waiting", "finished")
SPLITS = ("right", "below")
SCRIPTS = ("crew.py", "drive.py", "router.py")
# crew.py's old name, so an entry an older plugin version wrote still validates (recovery runs CREW)
LEGACY = "workers.py"
LEGACY_CLIENT = "claude"
CREW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "tmux", "scripts", "crew.py")
TOP = ("version", "holder", "cursor", "gen", "entries")
FIELDS = ("kind", "sid", "cwd", "resume", "note", "opener", "pane", "split", "split_from", "tui", "state", "started")
NOTE_MAX = 500
ROSTER_MAX = 1 << 20
EVENTS_MAX = 1 << 20
SETTLE = 1
BLOCK = 1 << 16
LOCK_TIMEOUT = 30
POLL = 0.1
CONTINUE = "Continue the unfinished task."


class RosterError(Exception):
    pass


class Held(RosterError):
    pass


class Stale(RosterError):
    pass


def directory(manager: str | None = None, *, proc=subprocess.run) -> str | None:
    if manager is None:
        try:
            manager = tmux_host.own_session(proc=proc)
        except tmux_host.TuiError as e:
            raise RosterError(f"{e}; give --manager <name>") from e
        if manager is None:
            return None
    elif not tmux_host.NAME.fullmatch(manager):
        raise RosterError(f"manager {manager!r}: want {tmux_host.NAME.pattern}")
    return os.path.join(os.path.abspath(os.path.expanduser(ROOT)), manager)


def _not_attached(directory: str) -> RosterError:
    return RosterError(f"manager directory {directory} not attached: run crew.py attach first")


def _checked(path: str, *, private: bool = False) -> None:
    try:
        st = os.lstat(path)
    except OSError as e:
        raise RosterError(f"manager directory {path}: {e.strerror}") from e
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise RosterError(f"manager directory {path}: not a directory owned by you")
    if private and st.st_mode & 0o077:
        mode = stat.S_IMODE(st.st_mode)
        raise RosterError(f"manager directory {path}: group or other permission bits set (mode {mode:04o})")


def ensure(directory: str) -> None:
    managers = os.path.dirname(directory)
    home = os.path.dirname(managers)
    try:
        os.makedirs(home, mode=0o700, exist_ok=True)
    except OSError as e:
        raise RosterError(f"manager directory {home}: {e.strerror}") from e
    # one level at a time, 0700 whatever the umask, each checked: nothing is created through a bad managers/
    for path in (managers, directory):
        try:
            try:
                os.mkdir(path, 0o700)
                os.chmod(path, 0o700)
            except FileExistsError:
                pass
        except OSError as e:
            raise RosterError(f"manager directory {path}: {e.strerror}") from e
        _checked(path, private=path == directory)


def events(directory: str, *, create: bool = True) -> str:
    directory = os.path.abspath(directory)
    path = os.path.join(directory, "events")
    if create:
        ensure(directory)
        try:
            return tmux_host.events_file(path)
        except tmux_host.TuiError as e:
            raise RosterError(str(e)) from e
    return path


def home(name: str | None, events: str | None, *, proc=subprocess.run) -> str | None:
    d = directory(name, proc=proc)
    if d is None or not os.path.isdir(d) or events is None:
        return None
    return d if os.path.realpath(events) == os.path.realpath(os.path.join(d, "events")) else None


def printable(text: str) -> bool:
    # so a printed field stays one table cell or one command
    return not any(unicodedata.category(c) == "Cc" or c in "\u2028\u2029" for c in text)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _or(words) -> str:
    return f"{', '.join(words[:-1])} or {words[-1]}"


def _match(pattern: re.Pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _text(value) -> bool:
    return isinstance(value, str) and printable(value)


_TIME = "an ISO 8601 time with a UTC offset"


def _time(value) -> bool:
    if not _text(value):
        return False
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _want(field: str, value, want) -> str:
    return f"{field}: {value!r}: want {want}"


def _keys(value: dict, want: tuple) -> str | None:
    missing, extra = sorted(set(want) - set(value), key=str), sorted(set(value) - set(want), key=str)
    parts = [*([f"missing {missing!r}"] if missing else []), *([f"unexpected {extra!r}"] if extra else [])]
    return f"keys: {', '.join(parts)}" if parts else None


_NAME_OR_NULL = (lambda v: v is None or _match(tmux_host.NAME, v), "a session name or null")
_CHECKS = {
    "kind": (lambda v: v in KINDS, _or(KINDS)),
    "sid": (lambda v: v is None or _match(SID, v), "a session id or null"),
    "cwd": (lambda v: _text(v) and os.path.isabs(v), "an absolute printable path"),
    "note": (lambda v: v is None or _text(v) and len(v) <= NOTE_MAX,
             f"null or a printable string of at most {NOTE_MAX} characters"),
    "opener": (lambda v: v is None or _match(tmux_host.OPENER, v),
               "a tmux session name or an iTerm2 pane id, or null"),
    "pane": (lambda v: v is None or _match(tmux_host.PANE, v) or _match(tmux_host.ITERM_ID, v),
             "a tmux pane id or an iTerm2 session id, or null"),
    "split": (lambda v: v is None or v in SPLITS, _or([*SPLITS, "null"])),
    "split_from": _NAME_OR_NULL,
    "tui": _NAME_OR_NULL,
    "state": (lambda v: v in STATES, _or(STATES)),
    "started": (_time, _TIME),
}


def _resume_fault(resume) -> str | None:
    if not (isinstance(resume, list) and len(resume) >= 2 and all(_text(x) for x in resume)):
        return _want("resume", resume, "a list of 2 or more printable strings")
    if not os.path.isabs(resume[0]):
        return _want("resume[0]", resume[0], "an absolute path")
    if not (os.path.isabs(resume[1]) and os.path.basename(resume[1]) in (*SCRIPTS, LEGACY)):
        return _want("resume[1]", resume[1], f"an absolute path named {_or(SCRIPTS)}")
    return None


def _value_fault(value) -> str | None:
    if not isinstance(value, dict):
        return _want("value", value, "an object")
    if bad := _keys(value, FIELDS):
        return bad
    for field in FIELDS:
        if field == "resume":
            bad = _resume_fault(value[field])
        else:
            ok, want = _CHECKS[field]
            bad = None if ok(value[field]) else _want(field, value[field], want)
        if bad:
            return bad
    return None


def _fault(name, value) -> str | None:
    if not _match(tmux_host.NAME, name):
        return f"name: want {tmux_host.NAME.pattern}"
    return _value_fault(value)


def _check(data, path: str, *, strict: bool) -> dict:
    def fail(why: str):
        raise RosterError(f"{path}: {why}")

    if not isinstance(data, dict):
        fail("want a JSON object")
    if bad := _keys(data, TOP):
        fail(bad)
    if type(data["version"]) is not int or data["version"] != VERSION:
        fail(_want("version", data["version"], VERSION))
    holder = data["holder"]
    if holder is not None:
        if not isinstance(holder, dict):
            fail("holder: want null or an object with session and since")
        if bad := _keys(holder, ("session", "since")):
            fail(f"holder: {bad}")
        if not _match(tmux_host.NAME, holder["session"]):
            fail(_want("holder.session", holder["session"], "a session name"))
        if not _time(holder["since"]):
            fail(_want("holder.since", holder["since"], _TIME))
    for field in ("cursor", "gen"):
        if type(data[field]) is not int or data[field] < 0:
            fail(_want(field, data[field], "a non-negative integer"))
    if not isinstance(data["entries"], dict):
        fail("entries: want an object")
    entries = {}
    for name, value in data["entries"].items():
        if bad := _fault(name, value):
            if strict:
                fail(f"entry {name!r}: {bad}")
            print(f"manager: roster.json: entry {name!r}: {bad}", file=sys.stderr)
        else:
            entries[name] = value
    return {**data, "entries": entries}


_NOT_REGULAR = "not a regular file owned by you"


def _open(path: str, flags: int) -> int | None:
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    except FileNotFoundError as e:
        if flags & os.O_CREAT:
            raise RosterError(f"{path}: {e.strerror}") from e
        return None
    except OSError as e:
        raise RosterError(f"{path}: {_NOT_REGULAR if e.errno == errno.ELOOP else e.strerror}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise RosterError(f"{path}: {_NOT_REGULAR}")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _locked(path: str) -> Iterator[None]:
    fd = _open(path, os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RosterError(f"{path}: busy") from None
                time.sleep(POLL)
            except OSError as e:
                raise RosterError(f"{path}: {e.strerror}") from e
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
        raise RosterError(f"{path}: over {ROSTER_MAX} bytes")
    try:
        return json.loads(raw)
    except RecursionError:
        raise RosterError(f"{path}: invalid JSON: nested too deep") from None
    except ValueError as e:
        raise RosterError(f"{path}: invalid JSON: {e}") from e


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _write(directory: str, path: str, text: str) -> None:
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix="roster.json.")
    except OSError as e:
        raise RosterError(f"{path}: {e.strerror}") from e
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise RosterError(f"{path}: {e.strerror}") from e


@contextlib.contextmanager
def locked(directory: str, *, write: bool = True) -> Iterator[dict]:
    # not reentrant: never call locked() or put() inside a block (a second flock waits for the first)
    directory = os.path.abspath(directory)
    if not os.path.lexists(directory):
        raise _not_attached(directory)
    _checked(os.path.dirname(directory))
    _checked(directory, private=True)
    path = os.path.join(directory, "roster.json")
    with _locked(os.path.join(directory, "roster.lock")):
        data = _read(path)
        missing = data is None
        r = _check({"version": VERSION, "holder": None, "cursor": 0, "gen": 0, "entries": {}} if missing else data,
                   path, strict=False)
        before = _dump(r)
        yield r
        if write:
            _check(r, path, strict=True)
            text = _dump(r)
            if missing or text != before:
                _write(directory, path, text)


def _live(session: str, proc) -> bool:
    try:
        res = proc(["tmux", "has-session", "-t", f"={session}"], capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)
    except OSError:
        return False
    return res.returncode == 0


def _holds(r: dict, directory: str, own: str | None, proc) -> bool:
    holder = r["holder"]
    if holder is None:
        return False
    if holder["session"] == own:
        return True
    if _live(holder["session"], proc):
        raise Held(f"manager directory {directory}: held by tmux session {holder['session']} since {holder['since']}; "
                   "ask that manager to run crew.py release, or end that session")
    return False


def take(r: dict, directory: str, own: str | None, *, proc=subprocess.run) -> None:
    if not _holds(r, directory, own, proc) and own is not None:
        r["holder"] = {"session": own, "since": now()}


def check(r: dict, directory: str, own: str | None, *, proc=subprocess.run) -> None:
    # the lease holds only for this block: a take-over can follow, so a later write must not assume it
    if not _holds(r, directory, own, proc) and own is not None:
        raise _not_attached(directory)


def entry(kind: str, *, sid: str | None, cwd: str, resume: list[str], note: str | None = None,
          opener: str | None = None, pane: str | None = None, split: str | None = None, split_from: str | None = None,
          tui: str | None = None, state: str = "working", started: str | None = None) -> dict:
    value = {"kind": kind, "sid": sid, "cwd": cwd, "resume": resume, "note": note, "opener": opener, "pane": pane,
             "split": split, "split_from": split_from, "tui": tui, "state": state,
             "started": now() if started is None else started}
    if bad := _value_fault(value):
        raise RosterError(f"entry: {bad}")
    return value


def set_entry(r: dict, name: str, value: dict) -> None:
    if bad := _fault(name, value):
        raise RosterError(f"entry {name!r}: {bad}")
    r["entries"][name] = value


def record(r: dict, name: str, value: dict) -> None:
    old = r["entries"].get(name)
    set_entry(r, name, value)
    if value["note"] is None and old is not None and old["sid"] is not None and old["sid"] == value["sid"]:
        r["entries"][name] = {**value, "note": old["note"]}


def put(directory: str, name: str, value: dict) -> None:
    with locked(directory) as r:
        record(r, name, value)


def unwritten(name: str, error: Exception) -> None:
    print(f"manager: entry {name} not written: {error}", file=sys.stderr)


def remove(r: dict, name: str) -> dict:
    try:
        return r["entries"].pop(name)
    except KeyError:
        raise RosterError(f"{name} not in roster") from None


def stop(r: dict, name: str, *, proc=subprocess.run) -> str:
    entries = r["entries"]
    key = name if name in entries else next((k for k in sorted(entries) if entries[k]["tui"] == name), None)
    if key is None:
        raise RosterError(f"{name} not in roster")
    for session in (key, entries[key]["tui"]):
        if session is None:
            continue
        with contextlib.suppress(tmux_host.TuiError):
            tmux_host.kill(session, proc=proc)
        if _live(session, proc):
            raise RosterError(f"{key}: session {session} still live after tmux kill-session")
    remove(r, key)
    return key


def recovery(entry: dict) -> list[str] | None:
    resume = list(entry["resume"])
    if os.path.basename(resume[1]) == LEGACY:
        resume[1] = CREW
    options = resume[:resume.index("--")] if "--" in resume else resume
    if entry["kind"] == "worker" and len(resume) > 3 and "--client" not in options:
        resume[4:4] = ["--client", LEGACY_CLIENT]
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
            raise RosterError(f"{path}: {e.strerror}") from e


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
        return 0 if f is None else sum(block.count(b"\n") for block in iter(lambda: f.read(BLOCK), b""))


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
    # a rotation then drops at least EVENTS_MAX // 2: never one for unhandled lines alone, nor per handled event
    path = events(directory, create=False)
    try:
        size = os.lstat(path).st_size
    except FileNotFoundError:
        return False
    except OSError as e:
        raise RosterError(f"{path}: {e.strerror}") from e
    return size > EVENTS_MAX and offset(path, r["cursor"]) >= EVENTS_MAX // 2


def advance(r: dict, directory: str, after: int | None, gen: int) -> int:
    if gen != r["gen"]:
        raise Stale(f"stale line numbers (gen {gen}, now {r['gen']}): run crew.py attach")
    path = events(directory, create=False)
    n = lines(path)
    if after is None:
        after = n
    elif after > n:
        raise Stale(f"line {after} is past the end of {path} ({n} lines): run crew.py attach")
    r["cursor"] = max(r["cursor"], after)
    return after


@contextlib.contextmanager
def _writing(path: str, flags: int) -> Iterator[int]:
    fd = _open(path, flags)
    if fd is None:
        raise RosterError(f"{path}: {os.strerror(errno.ENOENT)}")
    try:
        yield fd
    except OSError as e:
        raise RosterError(f"{path}: {e.strerror}") from e
    finally:
        os.close(fd)


def _refill(path: str, old: str, cursor: int, sleep) -> None:
    try:
        tmux_host.events_file(path)
    except tmux_host.TuiError as e:
        raise RosterError(str(e)) from e
    with _writing(path, os.O_WRONLY | os.O_NONBLOCK) as fd:
        os.fchmod(fd, 0o600)   # a writer's `>>` may have made it in between, with its umask
    # lets in-flight appends land in `old` before its lines are copied
    sleep(SETTLE)
    data = following(old, cursor)
    if data and not data.endswith(b"\n"):
        data += b"\n"
    with _writing(path, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK) as fd:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]


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
            raise RosterError(f"{p}: {e.strerror}") from e
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise RosterError(f"{p}: {_NOT_REGULAR}")
    try:
        os.replace(path, old)
    except OSError as e:
        raise RosterError(f"{path}: {e.strerror}") from e
    cursor = r["cursor"]
    # never raises after the rename: cursor and gen still change, so the new file is never numbered by the old
    try:
        _refill(path, old, cursor, sleep or time.sleep)
    except RosterError as e:
        print(f"manager: rotate: {e}: lines after {cursor} of {old} not copied", file=sys.stderr)
    r["cursor"], r["gen"] = 0, r["gen"] + 1
    return True
