"""A manager's durable roster, state machine, lease, and event read cursor."""

import contextlib
import datetime
import fcntl
import json
import os
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, replace

from agent_pm.manager.engine.events import Events
from agent_pm.manager.engine.types import Entry, Held, RosterError, Spec, Stale, State
from agent_pm.utils import Record, printable, read_regular, save

VERSION = 1
NOTE_MAX = 500
ROSTER_MAX = 1 << 20
LOCK_TIMEOUT = 30
POLL = 0.1


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _name(name: str) -> None:
    if not isinstance(name, str) or not name or printable(name) != name or "\t" in name:
        raise RosterError(f"name {name!r}: want nonempty printable text")


def _note(note: str | None) -> None:
    if note is not None and (
        not isinstance(note, str) or printable(note) != note or "\t" in note or len(note) > NOTE_MAX
    ):
        raise RosterError(f"note: want null or printable text of at most {NOTE_MAX} characters")


def _entry(data: dict) -> Entry:
    if set(data) != {"record", "state", "note", "started", "argv"}:
        raise ValueError("entry: invalid fields")
    record = None if data["record"] is None else Record.parse(json.dumps(data["record"]))
    state = State(data["state"])
    _note(data["note"])
    if (
        not isinstance(data["started"], str)
        or datetime.datetime.fromisoformat(data["started"]).utcoffset() is None
    ):
        raise ValueError("started: want an ISO 8601 time with a UTC offset")
    argv = data["argv"]
    if not isinstance(argv, list) or not all(isinstance(word, str) for word in argv):
        raise ValueError("argv: want a list of strings")
    if state in {State.STARTING, State.LOST}:
        if record is not None or not argv:
            raise ValueError("starting/lost: want an intent without a Record")
    elif record is None:
        raise ValueError(f"{state}: missing Record")
    return Entry(record, state, data["note"], data["started"], tuple(argv))


class Roster:
    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)
        self.path = os.path.join(self.directory, "roster.json")
        self._events = Events(self.directory)
        self._owner: str | None = None
        self._alive: Callable[[str], bool] = lambda _: False

    @property
    def events(self) -> str:
        with self._locked() as data:
            self._lease(data)
            self._events.ensure()
        return self._events.path

    def bind(self, owner: str | None, alive: Callable[[str], bool]) -> None:
        if owner is not None:
            _name(owner)
        self._owner, self._alive = owner, alive

    def _ensure(self) -> None:
        parent = os.path.dirname(self.directory)
        os.makedirs(os.path.dirname(parent), mode=0o700, exist_ok=True)
        for path in (parent, self.directory):
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
            else:
                os.chmod(path, 0o700, follow_symlinks=False)
            opened = os.lstat(path)
            if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid():
                raise RosterError(f"{path}: not a directory owned by you")
        if opened.st_mode & 0o077:
            raise RosterError(f"{self.directory}: group or other permission bits set")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict]:
        try:
            self._ensure()
            lock = os.path.join(self.directory, "roster.lock")
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
                    raise RosterError(f"{lock}: not a regular file owned by you")
                deadline = time.monotonic() + LOCK_TIMEOUT
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RosterError(f"{lock}: busy") from None
                        time.sleep(POLL)
                data = self._read()
                before = json.dumps(data, sort_keys=True)
                yield data
                if json.dumps(data, sort_keys=True) != before:
                    save(
                        self.path,
                        json.dumps(data, indent=2, sort_keys=True) + "\n",
                        own=True,
                        durable=True,
                    )
            finally:
                os.close(fd)
        except OSError as error:
            raise RosterError(str(error)) from error

    def _read(self) -> dict:
        raw = read_regular(self.path, limit=ROSTER_MAX + 1, own=True)
        if raw is None:
            if os.path.lexists(self.path):
                raise RosterError(f"{self.path}: not a readable regular file owned by you")
            return {"version": VERSION, "holder": None, "cursor": 0, "gen": 0, "entries": {}}
        if len(raw) > ROSTER_MAX:
            raise RosterError(f"{self.path}: over {ROSTER_MAX} bytes")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {
                "version",
                "holder",
                "cursor",
                "gen",
                "entries",
            }:
                raise ValueError("want roster fields")
            if type(data["version"]) is not int or data["version"] != VERSION:
                raise ValueError("version: want 1")
            for field in ("cursor", "gen"):
                if type(data[field]) is not int or data[field] < 0:
                    raise ValueError(f"{field}: want a nonnegative integer")
            holder = data["holder"]
            if holder is not None:
                if not isinstance(holder, dict) or set(holder) != {"session", "since"}:
                    raise ValueError("holder: want session and since")
                _name(holder["session"])
                if datetime.datetime.fromisoformat(holder["since"]).utcoffset() is None:
                    raise ValueError("holder.since: want UTC offset")
            if not isinstance(data["entries"], dict):
                raise ValueError("entries: want an object")
            for name, value in data["entries"].items():
                _name(name)
                if not isinstance(value, dict):
                    raise ValueError("entry: want an object")
                _entry(value)
        except (ValueError, TypeError, RecursionError) as error:
            raise RosterError(f"{self.path}: {error}") from error
        return data

    def _lease(self, data: dict) -> None:
        holder = data["holder"]
        if (
            holder is not None
            and holder["session"] != self._owner
            and self._alive(holder["session"])
        ):
            raise Held(f"{self.directory}: held by {holder['session']} since {holder['since']}")
        if self._owner is not None and (holder is None or holder["session"] != self._owner):
            raise RosterError(f"{self.directory}: not attached; run crew attach first")

    def hold(self, owner: str | None, alive: Callable[[str], bool]) -> None:
        self.bind(owner, alive)
        with self._locked() as data:
            holder = data["holder"]
            if holder is not None and holder["session"] != owner and alive(holder["session"]):
                raise Held(f"{self.directory}: held by {holder['session']} since {holder['since']}")
            if owner is not None and (holder is None or holder["session"] != owner):
                data["holder"] = {"session": owner, "since": _now()}

    def check_lease(self) -> None:
        with self._locked() as data:
            self._lease(data)

    def release(self) -> None:
        with self._locked() as data:
            self._lease(data)
            data["holder"] = None

    def entries(self) -> dict[str, Entry]:
        with self._locked() as data:
            return {name: _entry(value) for name, value in data["entries"].items()}

    def begin(self, intent: Spec) -> None:
        _name(intent.name)
        _note(intent.note)
        if (
            not intent.argv
            or not all(isinstance(word, str) for word in intent.argv)
            or not intent.argv[0]
        ):
            raise RosterError("argv: want a nonempty command")
        with self._locked() as data:
            self._lease(data)
            if intent.name in data["entries"]:
                raise RosterError(f"{intent.name}: already in roster")
            data["entries"][intent.name] = asdict(
                Entry(None, State.STARTING, intent.note, _now(), intent.argv)
            )

    def commit(self, name: str, record: Record) -> None:
        with self._locked() as data:
            self._lease(data)
            entry = self._get(data, name)
            if entry.state not in {State.STARTING, State.RESUMING}:
                raise RosterError(f"{name}: cannot commit {entry.state}")
            data["entries"][name] = asdict(replace(entry, record=record, state=State.LIVE, argv=()))

    def _get(self, data: dict, name: str) -> Entry:
        if name not in data["entries"]:
            raise RosterError(f"{name}: not in roster")
        return _entry(data["entries"][name])

    def _mark(self, data: dict, name: str, state: State) -> Entry:
        entry = self._get(data, name)
        allowed = {
            State.STARTING: {State.LOST},
            State.LIVE: {State.DONE, State.WAITING, State.GONE},
            State.GONE: {State.LIVE, State.DONE, State.WAITING, State.RESUMING},
            State.RESUMING: {State.GONE},
            State.DONE: set(),
            State.WAITING: set(),
            State.LOST: set(),
        }
        if state != entry.state and state not in allowed[entry.state]:
            raise RosterError(f"{name}: cannot change {entry.state} to {state}")
        started = _now() if state == State.RESUMING and entry.state != state else entry.started
        data["entries"][name] = asdict(replace(entry, state=state, started=started))
        return entry

    def mark(self, name: str, state: State) -> None:
        with self._locked() as data:
            self._lease(data)
            try:
                target = State(state)
            except ValueError as error:
                raise RosterError(f"{name}: unknown state {state!r}") from error
            self._mark(data, name, target)

    def claim_resume(self, name: str) -> Entry | None:
        with self._locked() as data:
            self._lease(data)
            entry = self._get(data, name)
            if entry.state in {State.RESUMING, State.LIVE}:
                return None
            if entry.state != State.GONE:
                raise RosterError(f"{name}: cannot resume {entry.state}")
            return self._mark(data, name, State.RESUMING)

    def remove(self, name: str) -> None:
        with self._locked() as data:
            self._lease(data)
            self._get(data, name)
            del data["entries"][name]

    def note(self, name: str, text: str | None) -> None:
        _note(text)
        with self._locked() as data:
            self._lease(data)
            data["entries"][name] = asdict(replace(self._get(data, name), note=text))

    def adopt(self, record: Record) -> str:
        _name(record.name)
        with self._locked() as data:
            self._lease(data)
            for name, value in data["entries"].items():
                current = _entry(value)
                if current.record is not None and (
                    current.record.name == record.name
                    or set(current.record.sessions) & set(record.sessions)
                ):
                    return name
            old = data["entries"].get(record.name)
            if old is not None:
                entry = _entry(old)
                if entry.state != State.LOST:
                    return record.name
                entry = replace(entry, record=record, state=State.LIVE, argv=())
            else:
                entry = Entry(record, State.LIVE, None, _now())
            data["entries"][record.name] = asdict(entry)
            return record.name

    def backlog(self) -> tuple[int, int, list[str]]:
        with self._locked() as data:
            return data["cursor"], data["gen"], self._events.following(data["cursor"])

    def _cursor(self, data: dict, after: int | None, gen: int) -> int:
        if gen != data["gen"]:
            raise Stale(f"stale line numbers (gen {gen}, now {data['gen']}): run crew attach")
        count = self._events.count()
        value = count if after is None else after
        if type(value) is not int or value < 0 or value > count:
            raise Stale(f"line {value} is outside events ({count} lines): run crew attach")
        return value

    def advance(self, after: int | None, gen: int) -> int:
        with self._locked() as data:
            self._lease(data)
            value = self._cursor(data, after, gen)
            data["cursor"] = max(data["cursor"], value)
            return value

    def next_event(self, after: int | None, gen: int) -> tuple[int, str | None]:
        with self._locked() as data:
            value = self._cursor(data, after, gen)
            if self._events.due(data["cursor"]):
                raise Stale("events rotation due: run crew attach")
            lines = self._events.following(value)
            return (value + 1, lines[0]) if lines else (value, None)

    def rotate_events(self) -> bool:
        with self._locked() as data:
            self._lease(data)
            if not self._events.rotate(data["cursor"]):
                return False
            data["cursor"], data["gen"] = 0, data["gen"] + 1
            return True
