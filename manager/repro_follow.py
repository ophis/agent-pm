#!/usr/bin/env python3
"""TASK-854 standalone POSIX replay: python3 -I manager/repro_follow.py --rounds 3.

Standard library only; all data stays in one TemporaryDirectory per round. No installed
agent-pm, credentials, clients, board, or real HOME. Reader methods below are verbatim
snapshots, checked against the repo by unit tests. The minimal ledger implements the
same stable roster/events flock order; close/issue only remove/add fake entries.
The real CLI/launcher sequence is separately covered by crew_cli_integration_test.py.
This cannot reproduce the client Monitor itself. A PASS excludes only this replay.
"""

import argparse
import contextlib
import errno
import fcntl
import json
import multiprocessing
import os
import platform
import re
import select
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

EVENTS_MAX = 1 << 20
EVENT = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2} \S+ \S.*")


class RosterError(Exception):
    pass


class Stale(RosterError):
    pass


class StaleHolder(Stale):
    pass


def _add_recovery(error, _args, _cursor):
    error.add_note("Standalone replay has no holder; inspect the temporary ledger diagnostics.")


def read_regular(
    path: str | os.PathLike,
    *,
    offset: int = 0,
    limit: int | None = None,
    tail: bool = False,
) -> bytes | None:
    """From byte `offset`: to the end, else the first `limit` bytes (the last with `tail`); None: no
    regular file, an OSError. Never waits on a FIFO.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    return _read_fd(fd, offset, limit, tail=tail)


def _read_fd(fd: int, offset: int, limit: int | None, *, tail: bool) -> bytes | None:
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            f.seek(offset)
            if limit is None:
                return f.read()
            if tail:
                f.seek(max(f.seek(0, os.SEEK_END) - limit, offset))
            return f.read(limit)
    except OSError:
        return None
    finally:
        os.close(fd)


@contextlib.contextmanager
def file_lock(path: str | Path, *, wait: bool = True) -> Iterator[None]:
    """Exclusive stable <path>.lock, non-inheritable; held, `wait=False` raises BlockingIOError.
    Event writers acquire it before opening the event file and hold it through descriptor close,
    without acquiring a roster lock. Rotators hold it through physical changes and durable ledger
    save; never replace this lock.
    """
    lock = str(path) + ".lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(errno.EINVAL, "not a regular lock file", lock)
        fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def sync_directory(path: str | Path) -> None:
    """Persist directory entries after rename or unlink; filesystem errors propagate."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_line(
    path: str | Path,
    text: str | bytes,
    *,
    durable: bool = False,
    locked: bool = False,
) -> None:
    """Refuses anything but a regular file, never waits on a FIFO; `durable` syncs it and its
    directory; `locked` uses file_lock.
    """
    data = memoryview(text.encode(errors="replace") if isinstance(text, str) else text)
    with file_lock(path) if locked else contextlib.nullcontext():
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, "not a regular file", str(path))
            while data:
                data = data[os.write(fd, data) :]
            if durable:
                os.fsync(fd)
        finally:
            os.close(fd)
        if durable:
            sync_directory(os.path.dirname(os.path.abspath(path)))


def printable(text: str) -> str:
    """Without control characters but tabs."""
    return "".join(c for c in text if c.isprintable() or c == "\t")


def _identity(path: str) -> tuple[int, int]:
    opened = os.stat(path)
    if not stat.S_ISREG(opened.st_mode):
        raise OSError(f"{path}: not a regular file")
    return opened.st_dev, opened.st_ino


class Events:
    def __init__(self, directory):
        self.path = str(Path(directory, "events"))

    def identity(self) -> tuple[int, int] | None:
        try:
            return _identity(self.path)
        except FileNotFoundError:
            return None

    def read(self) -> bytes:
        data = read_regular(self.path)
        if data is None:
            if os.path.lexists(self.path):
                raise OSError(f"{self.path}: not a readable regular file")
            return b""
        return data

    def following(self, after: int) -> list[str]:
        data = self.read().split(b"\n")[:-1]
        return [line.decode(errors="replace") for line in data[after:]]

    def count(self) -> int:
        return self.read().count(b"\n")

    def _due(self, data: bytes, cursor: int) -> bool:
        retained = data.split(b"\n", cursor)[-1]
        return len(data) > EVENTS_MAX and len(data) - len(retained) >= EVENTS_MAX // 2

    def due(self, cursor: int) -> bool:
        return self._due(self.read(), cursor)


class Roster:
    def __init__(self, directory):
        self.directory = directory
        self._events = Events(directory)

    @property
    def events_path(self):
        return self._events.path

    @contextlib.contextmanager
    def _locked(self):
        # No holder/receipt/rotation in this small replay; fake run actions share these locks.
        with file_lock(Path(self.directory, "roster")), file_lock(self._events.path):
            ledger = Path(self.directory, "roster.json")
            data = json.loads(ledger.read_text())
            before = json.dumps(data)
            yield data
            if json.dumps(data) != before:
                candidate = ledger.with_suffix(".next")
                candidate.write_text(json.dumps(data))
                os.replace(candidate, ledger)

    def _lease(self, _data):
        pass

    def _cursor(self, data: dict, after: int | None, gen: int) -> int:
        if gen != data["gen"]:
            raise Stale(f"stale line numbers (gen {gen}, now {data['gen']}): run crew attach")
        count = self._events.count()
        value = count if after is None else after
        if type(value) is not int or value < 0 or value > count:
            raise Stale(f"line {value} is outside events ({count} lines): run crew attach")
        return value

    def watch_events(
        self, after: int | None, gen: int, *, acknowledge: bool = True
    ) -> tuple[int, tuple[int, int] | None]:
        with self._locked() as data:
            self._lease(data)
            value = self._cursor(data, after, gen)
            if acknowledge:
                data["cursor"] = max(data["cursor"], value)
            return value, self._events.identity()

    def event_identity(self) -> tuple[int, int] | None:
        with self._locked():
            return self._events.identity()

    def next_event(
        self, after: int | None, gen: int, *, identity: tuple[int, int] | None = None
    ) -> tuple[int, str | None]:
        with self._locked() as data:
            self._lease(data)
            if identity is not None and identity != self._events.identity():
                raise Stale("events file replaced: run crew attach")
            value = self._cursor(data, after, gen)
            if self._events.due(data["cursor"]):
                raise Stale("events rotation due: run crew attach")
            lines = self._events.following(value)
            if identity is not None and identity != self._events.identity():
                raise Stale("events file replaced: run crew attach")
            return (value + 1, lines[0]) if lines else (value, None)


class Crew:
    def __init__(self, roster):
        self.roster = roster
        self._listed = None

    def event_content(self, _event):
        raise RuntimeError("content is not part of this replay")

    def next_event(self, *, after: int | None, gen: int) -> tuple[int, str]:
        self._listed = None
        cursor, identity = self.roster.watch_events(after, gen, acknowledge=False)
        while True:
            if identity is None:
                identity = self.roster.event_identity()
            line, event = self.roster.next_event(cursor, gen, identity=identity)
            if event is not None:
                if not EVENT.fullmatch(event):
                    raise RosterError(f"{self.roster.events_path}: invalid event at line {line}")
                return line, printable(event)
            else:
                time.sleep(0.5)


def _crew(args):
    return Crew(Roster(args.directory))


def _next_event(args) -> int:
    crew = _crew(args)
    after = args.after
    try:
        while True:
            after, event = crew.next_event(after=after, gen=args.gen)
            block = f"{after} {event}\n"
            if args.content:
                block += crew.event_content(event)
            # One write per event: Monitor groups lines written within 200 ms into one notification.
            print(block, end="", flush=True)
            if not args.follow:
                return 0
    except StaleHolder as error:
        _add_recovery(error, args, None if after is None else (after, args.gen))
        raise


def _child(directory, action, value, pipe=None, *, locked=False):
    if action == "follow":
        read_fd, write_fd = pipe
        os.close(read_fd)
        os.dup2(write_fd, 1)
        os.close(write_fd)

        def trace(frame, event, _arg):
            if event == "return" and frame.f_code is Roster.next_event.__code__:
                Path(directory, "poll.json").write_text(
                    json.dumps(
                        {key: frame.f_locals.get(key) for key in ("after", "value", "lines")}
                    )
                )
            return trace

        sys.settrace(trace)
        _next_event(
            argparse.Namespace(directory=directory, after=3320, gen=0, content=False, follow=True)
        )
        return
    if action == "append":
        # Production SessionStatusLog defaults to unlocked, unsynced append_line.
        append_line(Path(directory, "events"), value, locked=locked)
        return
    roster = Roster(directory)
    if action == "ack":
        roster.watch_events(int(value), 0)
    else:
        with roster._locked() as data:
            if action == "close":
                del data["entries"][value]
            elif action == "issue":
                data["entries"][value] = "fake run"
            else:
                raise ValueError(action)


def _diagnostics(directory):
    path = Path(directory, "events")
    info = path.stat()
    poll = Path(directory, "poll.json")
    # The trace file can be mid-write. It is diagnostic only, never a gate.
    try:
        reader = json.loads(poll.read_text())
    except (FileNotFoundError, ValueError):
        reader = "not available"
    return {
        "identity": [info.st_dev, info.st_ino],
        "bytes": info.st_size,
        "lines": path.read_bytes().count(b"\n"),
        "ledger": json.loads(Path(directory, "roster.json").read_text()),
        "reader": reader,
    }


def replay(timeout, split_tail, *, locked=False):
    # Both macOS and Linux support fork; no background service or shared user state.
    context = multiprocessing.get_context("fork")
    with tempfile.TemporaryDirectory(prefix="task-854-") as directory:
        path = Path(directory, "events")
        path.write_text("12:00:00 history done\n" * 3320)
        Path(directory, "roster.json").write_text(
            json.dumps(
                {"cursor": 0, "gen": 0, "entries": {"one": "fake worker", "two": "fake worker"}}
            )
        )
        identity = (path.stat().st_dev, path.stat().st_ino)

        def run(action, value):
            child = context.Process(
                target=_child, args=(directory, action, value), kwargs={"locked": locked}
            )
            child.start()
            child.join(timeout)
            if child.is_alive():
                child.terminate()
                child.join(5)
                raise RuntimeError(f"{action} timed out")
            if child.exitcode != 0:
                raise RuntimeError(f"{action} exited {child.exitcode}")

        read_fd, write_fd = os.pipe()
        reader = context.Process(target=_child, args=(directory, "follow", "", (read_fd, write_fd)))
        reader.start()
        os.close(write_fd)
        buffered = b""

        def expect(number, event):
            nonlocal buffered
            started = time.monotonic()
            expected = f"{number} {event}\n".encode()
            while len(buffered) < len(expected):
                remaining = max(0, timeout - (time.monotonic() - started))
                if not select.select([read_fd], [], [], remaining)[0]:
                    raise RuntimeError(f"no output for line {number}")
                block = os.read(read_fd, 65536)
                if not block:
                    raise RuntimeError(f"reader exited {reader.exitcode}")
                buffered += block
            if buffered[: len(expected)] != expected:
                raise RuntimeError(f"unexpected output: {buffered!r}")
            buffered = buffered[len(expected) :]
            print(
                json.dumps(
                    {"received": number, "latency_ms": round((time.monotonic() - started) * 1000)}
                ),
                flush=True,
            )

        try:
            for number in range(3321, 3326):
                event = f"23:51:06 worker-{number} done"
                run("append", event + "\n")
                expect(number, event)
            run("ack", "3325")
            for worker in ("one", "two"):
                run("close", worker)
            for issue in ("TASK-852", "TASK-853"):
                run("issue", issue)
            print(json.dumps({"after_operations": _diagnostics(directory)}), flush=True)
            event = "23:56:58 engineer-TASK-850-5d2a1a93 done"
            run("append", event if split_tail else event + "\n")
            if split_tail:
                if select.select([read_fd], [], [], 0.6)[0]:
                    raise RuntimeError("reader emitted unfinished line")
                run("append", "\n")
            expect(3326, event)
            if buffered or select.select([read_fd], [], [], 0.6)[0]:
                raise RuntimeError("unexpected extra output or reader exit")
            if not reader.is_alive():
                raise RuntimeError(f"reader exited {reader.exitcode}")
            if identity != (path.stat().st_dev, path.stat().st_ino):
                raise RuntimeError("file identity changed")
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "split_tail": split_tail,
                        "writer_locked": locked,
                        "final": _diagnostics(directory),
                    }
                ),
                flush=True,
            )
        except Exception:
            print(
                json.dumps({"result": "FAIL", "diagnostics": _diagnostics(directory)}), flush=True
            )
            raise
        finally:
            reader.terminate()
            reader.join(5)
            if reader.is_alive():
                reader.kill()
                reader.join(5)
            os.close(read_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument(
        "--locked",
        action="store_true",
        help="additional control: lock writers (default matches production: unlocked)",
    )
    args = parser.parse_args()
    if args.rounds < 1 or not 1 <= args.timeout <= 60:
        parser.error("rounds must be positive; timeout must be between 1 and 60 seconds")
    print(
        json.dumps(
            {
                "system": platform.system(),
                "release": platform.release(),
                "python": platform.python_version(),
                "rounds": args.rounds,
                "writer_locked": args.locked,
            }
        ),
        flush=True,
    )
    for _ in range(args.rounds):
        replay(args.timeout, split_tail=False, locked=args.locked)
        replay(args.timeout, split_tail=True, locked=args.locked)


if __name__ == "__main__":
    main()
