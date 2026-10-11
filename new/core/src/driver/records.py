"""Run records: <workdir>/run.jsonl (CHANNEL), one JSON object {"ts", "kind", **fields} per line, `ts` local ISO 8601
to the millisecond. The Driver writes `session`, `client_session` and `stderr`; the Delegate writes the rest through
this module. A write refuses a symlink or anything but a regular file; a read never follows one and returns what it
can. The agent run controls its workdir and can append to the file: what a read finds is a status hint, never an
authorization or a recovery grant. Imports only the stdlib."""
import errno
import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path

CHANNEL = "run.jsonl"


def path(workdir: str) -> str:
    """The run record of `workdir`: <abspath workdir>/run.jsonl."""
    return os.path.join(os.path.abspath(workdir), CHANNEL)


def stamp() -> str:
    """Now, local, as ISO 8601 with offset, to the millisecond."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def holds(path: str | Path, text: str) -> bool:
    """Whether the regular file at `path` holds exactly `text`; a symlink, a FIFO, a missing file or any error is no."""
    data = text.encode()
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
            return stat.S_ISREG(os.fstat(f.fileno()).st_mode) and f.read(len(data) + 1) == data
    except OSError:
        return False


def append_line(path: str | Path, text: str, *, durable: bool = False) -> None:
    """Appends `text` to `path` (created 0600) in one write, refusing a symlink or anything but a regular file (a FIFO
    would block). A lone surrogate is written as `?`."""
    data = memoryview(text.encode(errors="replace"))
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        while data:
            data = data[os.write(fd, data):]
        if durable:
            os.fsync(fd)
    finally:
        os.close(fd)
    if durable:
        parent = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


def note(path: str, kind: str, *, best_effort: bool = False, durable: bool = False, **fields) -> None:
    """Appends record `kind` to the run record at `path` as one line {"ts", "kind", **fields}; durable syncs it to disk;
    with best_effort an OSError is printed, never raised."""
    try:
        append_line(path, json.dumps({"ts": stamp(), "kind": kind, **fields}, ensure_ascii=False) + "\n",
                    durable=durable)
    except OSError as e:
        if not best_effort:
            raise
        print(f"records: {e}", file=sys.stderr)


def read(path: str) -> list[dict]:
    """The JSON-object lines of the run record at `path`, in order; any other line is skipped. A symlink, anything but a
    regular file, or any OSError is []."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return []
            lines = f.read().split(b"\n")
    except OSError:
        return []
    found = []
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            found.append(record)
    return found


def session(workdir: str, sid: str) -> dict | None:
    """The latest `session` record of `sid` in <workdir>/run.jsonl (read()) that names a printable absolute cwd and
    project as a bool; else None. No other field is checked."""
    found = None
    for e in read(path(workdir)):
        if (e.get("kind") == "session" and e.get("sid") == sid and isinstance(e.get("cwd"), str)
                and os.path.isabs(e["cwd"]) and e["cwd"].isprintable() and isinstance(e.get("project"), bool)):
            found = e
    return found


def complete(workdir: str) -> bool:
    """Whether the current attempt in <workdir>/run.jsonl (read()) is complete: a `result` record follows the last
    `session` record. No `session` record is no."""
    done = None
    for e in read(path(workdir)):
        if e.get("kind") == "session":
            done = False
        elif e.get("kind") == "result" and done is False:
            done = True
    return done is True
