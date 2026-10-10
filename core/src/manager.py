"""A manager's directory, ~/.agent-pm/managers/<name>/: the fixed home of its `events` file, which workers.py (start,
next-event), drive.py (--runner tui, --detach) and router.py --tui use when given no --events (all but next-event
append to it), and of its roster, roster.json, written only under the flock on roster.lock (both 0600).

<name> is [A-Za-z0-9_-]+, resolved (directory) as `--manager <name>`, else the caller's tmux session
(tui_claude.own_session), else none. `workers.py attach` creates it (ensure) and `events` (0600, a regular file of the
caller's: tui_claude.events_file); `drive.py` and `router.py --tui` still may. ~/.agent-pm is made when missing and not
checked; managers/ and <name>/ are made one level at a time, 0700 whatever the umask. One that exists keeps its mode but
must be a directory (not a symlink) owned by the caller, and <name>/ also with no group or other permission bits (every
roster access checks), else ManagerError, so nothing is created through a bad managers/. Every failure is a
ManagerError.

roster.json: a regular file of the caller's, at most ROSTER_MAX bytes (a symlink, a FIFO or a foreign file is refused),
JSON (indent 2, sorted keys), written through a temp file and os.replace. Missing: version 1, holder null, cursor 0,
gen 0, no entries.
  {"version": 1, "holder": null | {"session": NAME, "since": time}, "cursor": int >= 0, "gen": int >= 0,
   "entries": {NAME: entry}}
Exactly these keys (holder's: session, since), else ManagerError and the file untouched. An entry has exactly these
keys, else it is skipped with one stderr line `manager: roster.json: entry <name>: <field>: <why>` (a later write drops
it):
  kind              worker | role | pipeline
  sid               SID or null
  cwd               an absolute path, printable
  resume            a list of 2 or more printable strings; [0] an absolute path, [1] one to workers.py, drive.py or
                    router.py
  note              null, or printable with at most NOTE_MAX characters
  opener            tui_claude.OPENER or null
  pane              tui_claude.PANE or tui_claude.ITERM_ID, or null
  split             right | below | null
  split_from, tui   NAME or null
  state             working | done | blocked | dead | gone | finished
  started           time
time: an ISO 8601 string with a UTC offset, printable. printable: no control character (Unicode Cc) and no U+2028 or
U+2029, so a printed field stays one table cell or one command.
"""
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
from datetime import datetime, timezone

import tui_claude

ROOT = "~/.agent-pm/managers"
SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
VERSION = 1
KINDS = ("worker", "role", "pipeline")
STATES = ("working", "done", "blocked", "dead", "gone", "finished")
SPLITS = ("right", "below")
SCRIPTS = ("workers.py", "drive.py", "router.py")
TOP = ("version", "holder", "cursor", "gen", "entries")
FIELDS = ("kind", "sid", "cwd", "resume", "note", "opener", "pane", "split", "split_from", "tui", "state", "started")
NOTE_MAX = 500
ROSTER_MAX = 1 << 20
LOCK_TIMEOUT = 30
POLL = 0.1
CONTINUE = "Continue the unfinished task."


class ManagerError(Exception):
    pass


class Held(ManagerError):
    """Another live tmux session holds the directory."""


def directory(manager: str | None = None, *, proc=subprocess.run) -> str | None:
    """The absolute path of the manager's directory, the filesystem untouched; None outside tmux without `manager`."""
    if manager is None:
        try:
            manager = tui_claude.own_session(proc=proc)
        except tui_claude.TuiError as e:
            raise ManagerError(f"{e}; give --manager <name>") from e
        if manager is None:
            return None
    elif not tui_claude.NAME.fullmatch(manager):
        raise ManagerError(f"manager {manager!r}: want {tui_claude.NAME.pattern}")
    return os.path.join(os.path.abspath(os.path.expanduser(ROOT)), manager)


def _not_attached(directory: str) -> ManagerError:
    return ManagerError(f"manager directory {directory} not attached: run workers.py attach first")


def _checked(path: str, *, private: bool = False) -> None:
    """path must be a directory (not a symlink) owned by the caller; private: no group or other permission bits."""
    try:
        st = os.lstat(path)
    except OSError as e:
        raise ManagerError(f"manager directory {path}: {e.strerror}") from e
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise ManagerError(f"manager directory {path}: not a directory owned by you")
    if private and st.st_mode & 0o077:
        mode = stat.S_IMODE(st.st_mode)
        raise ManagerError(f"manager directory {path}: group or other permission bits set (mode {mode:04o})")


def ensure(directory: str) -> None:
    """Creates directory (as `directory()` returns it) and checks it."""
    managers = os.path.dirname(directory)
    home = os.path.dirname(managers)
    try:
        os.makedirs(home, mode=0o700, exist_ok=True)
    except OSError as e:
        raise ManagerError(f"manager directory {home}: {e.strerror}") from e
    for path in (managers, directory):
        try:
            try:
                os.mkdir(path, 0o700)
                os.chmod(path, 0o700)
            except FileExistsError:
                pass
        except OSError as e:
            raise ManagerError(f"manager directory {path}: {e.strerror}") from e
        _checked(path, private=path == directory)


def events(directory: str, *, create: bool = True) -> str:
    """The absolute path of directory's `events` file; with create, the directory and the file are made first."""
    directory = os.path.abspath(directory)
    path = os.path.join(directory, "events")
    if create:
        ensure(directory)
        try:
            return tui_claude.events_file(path)
        except tui_claude.TuiError as e:
            raise ManagerError(str(e)) from e
    return path


def printable(text: str) -> bool:
    """No control character (Unicode Cc) and no U+2028 or U+2029: the text stays one table cell or one command."""
    return not any(unicodedata.category(c) == "Cc" or c in "\u2028\u2029" for c in text)


def now() -> str:
    """The time as this code writes it: ISO 8601, UTC, to the second."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


_NAME_OR_NULL = (lambda v: v is None or _match(tui_claude.NAME, v), "a session name or null")
_CHECKS = {
    "kind": (lambda v: v in KINDS, _or(KINDS)),
    "sid": (lambda v: v is None or _match(SID, v), "a session id or null"),
    "cwd": (lambda v: _text(v) and os.path.isabs(v), "an absolute printable path"),
    "note": (lambda v: v is None or _text(v) and len(v) <= NOTE_MAX,
             f"null or a printable string of at most {NOTE_MAX} characters"),
    "opener": (lambda v: v is None or _match(tui_claude.OPENER, v),
               "a tmux session name or an iTerm2 pane id, or null"),
    "pane": (lambda v: v is None or _match(tui_claude.PANE, v) or _match(tui_claude.ITERM_ID, v),
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
    if not (os.path.isabs(resume[1]) and os.path.basename(resume[1]) in SCRIPTS):
        return _want("resume[1]", resume[1], f"an absolute path named {_or(SCRIPTS)}")
    return None


def _value_fault(value) -> str | None:
    """Why `value` is not an entry, as `<field>: <why>`; None when it is."""
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
    if not _match(tui_claude.NAME, name):
        return f"name: want {tui_claude.NAME.pattern}"
    return _value_fault(value)


def _check(data, path: str, *, strict: bool) -> dict:
    """data as a roster: a top-level fault is a ManagerError. An invalid entry is skipped with a stderr line (strict: a
    ManagerError). Returns the roster with the valid entries only."""
    def fail(why: str):
        raise ManagerError(f"{path}: {why}")

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
        if not _match(tui_claude.NAME, holder["session"]):
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
    """An fd of path, a regular file of the caller's, never through a symlink; else ManagerError. None when missing and
    not O_CREAT."""
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    except FileNotFoundError as e:
        if flags & os.O_CREAT:
            raise ManagerError(f"{path}: {e.strerror}") from e
        return None
    except OSError as e:
        raise ManagerError(f"{path}: {_NOT_REGULAR if e.errno == errno.ELOOP else e.strerror}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            raise ManagerError(f"{path}: {_NOT_REGULAR}")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _locked(path: str) -> Iterator[None]:
    """The exclusive flock on path, tried every POLL seconds for LOCK_TIMEOUT."""
    fd = _open(path, os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ManagerError(f"{path}: busy") from None
                time.sleep(POLL)
            except OSError as e:
                raise ManagerError(f"{path}: {e.strerror}") from e
        yield
    finally:
        os.close(fd)


def _read(path: str):
    """roster.json parsed; None when missing."""
    fd = _open(path, os.O_RDONLY | os.O_NONBLOCK)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as f:
        raw = f.read(ROSTER_MAX + 1)
    if len(raw) > ROSTER_MAX:
        raise ManagerError(f"{path}: over {ROSTER_MAX} bytes")
    try:
        return json.loads(raw)
    except RecursionError:
        raise ManagerError(f"{path}: invalid JSON: nested too deep") from None
    except ValueError as e:
        raise ManagerError(f"{path}: invalid JSON: {e}") from e


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _write(directory: str, path: str, text: str) -> None:
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix="roster.json.")
    except OSError as e:
        raise ManagerError(f"{path}: {e.strerror}") from e
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise ManagerError(f"{path}: {e.strerror}") from e


@contextlib.contextmanager
def roster(directory: str, *, write: bool = True) -> Iterator[dict]:
    """Under roster.lock, yields directory's roster (a fresh one when roster.json is missing). Never creates directory:
    missing is a ManagerError. With write, a normal exit validates the whole dict (a fault: ManagerError, nothing written)
    and writes it when it changed or roster.json was missing; an exception in the block, or write=False, writes
    nothing. Not reentrant: never call roster() or put() inside a block (a second flock waits for the first)."""
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
    """Whether tmux has the session; no tmux (OSError) counts as not live."""
    try:
        res = proc(["tmux", "has-session", "-t", f"={session}"], capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)
    except OSError:
        return False
    return res.returncode == 0


def _holds(r: dict, directory: str, own: str | None, proc) -> bool:
    """Whether own is the holder; Held when another, live session is (own None included)."""
    holder = r["holder"]
    if holder is None:
        return False
    if holder["session"] == own:
        return True
    if _live(holder["session"], proc):
        raise Held(f"manager directory {directory}: held by tmux session {holder['session']} since {holder['since']}; "
                   "ask that manager to run workers.py release, or end that session")
    return False


def take(r: dict, directory: str, own: str | None, *, proc=subprocess.run) -> None:
    """attach's lease, inside a roster() block. Free: no holder, own, or a holder whose tmux session is gone; then own
    becomes the holder (`since` kept when it already was). Held by another live session: Held. own None (outside tmux)
    never changes the holder."""
    if not _holds(r, directory, own, proc) and own is not None:
        r["holder"] = {"session": own, "since": now()}


def check(r: dict, directory: str, own: str | None, *, proc=subprocess.run) -> None:
    """Every other command's lease, inside a roster() block: Held by another live session; not attached when own (not
    None) is not the holder; own None passes. It holds only for that block: a take-over can follow, so a later write
    must not assume it."""
    if not _holds(r, directory, own, proc) and own is not None:
        raise _not_attached(directory)


def entry(kind: str, *, sid: str | None, cwd: str, resume: list[str], note: str | None = None,
          opener: str | None = None, pane: str | None = None, split: str | None = None, split_from: str | None = None,
          tui: str | None = None, state: str = "working", started: str | None = None) -> dict:
    """A valid roster entry (`started` defaults to now); ManagerError when it is not."""
    value = {"kind": kind, "sid": sid, "cwd": cwd, "resume": resume, "note": note, "opener": opener, "pane": pane,
             "split": split, "split_from": split_from, "tui": tui, "state": state,
             "started": now() if started is None else started}
    if bad := _value_fault(value):
        raise ManagerError(f"entry: {bad}")
    return value


def set_entry(r: dict, name: str, value: dict) -> None:
    """Validates value and sets (replacing) entries[name], inside a roster() block."""
    if bad := _fault(name, value):
        raise ManagerError(f"entry {name!r}: {bad}")
    r["entries"][name] = value


def put(directory: str, name: str, value: dict) -> None:
    """roster() + set_entry, for a caller in no roster() block."""
    with roster(directory) as r:
        set_entry(r, name, value)


def remove(r: dict, name: str) -> dict:
    """Deletes and returns entries[name], inside a roster() block."""
    try:
        return r["entries"].pop(name)
    except KeyError:
        raise ManagerError(f"{name} not in roster") from None


def recovery(entry: dict) -> list[str] | None:
    """The argv that resumes the entry, run in entry["cwd"]: a worker's or pipeline's `resume`; a role's plus its sid
    and `--resume`; None for a role without a sid."""
    resume = list(entry["resume"])
    if entry["kind"] != "role":
        return resume
    if entry["sid"] is None:
        return None
    return [*resume, "--sid", entry["sid"], "--resume", "--input", CONTINUE]
