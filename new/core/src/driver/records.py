"""Run records: <workdir>/run.jsonl, one JSON object {"ts", "kind", **fields} per line. Files open nonblocking (a FIFO
must not block) and never through a symlink; a write refuses anything but a regular file, a read returns what it can.
The agent run can append to the file: what a read finds is a status hint, never an authorization or a recovery grant."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from utils.files import append_line, read_regular
from utils.paths import printable_abs

CHANNEL = "run.jsonl"


def path(workdir: str) -> str:
    return os.path.join(os.path.abspath(workdir), CHANNEL)


def stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def holds(path: str | Path, text: str) -> bool:
    data = text.encode()
    return read_regular(path, limit=len(data) + 1) == data


def note(
    path: str, kind: str, *, best_effort: bool = False, durable: bool = False, **fields
) -> None:
    try:
        append_line(
            path,
            json.dumps({"ts": stamp(), "kind": kind, **fields}, ensure_ascii=False) + "\n",
            durable=durable,
        )
    except OSError as e:
        if not best_effort:
            raise
        print(f"records: {e}", file=sys.stderr)


def read(path: str) -> list[dict]:
    data = read_regular(path)
    if data is None:
        return []
    found = []
    for line in data.split(b"\n"):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict):
            found.append(record)
    return found


def session(workdir: str, sid: str) -> dict | None:
    found = None
    for e in read(path(workdir)):
        if (
            e.get("kind") == "session"
            and e.get("sid") == sid
            and printable_abs(e.get("cwd"))
            and isinstance(e.get("project"), bool)
        ):
            found = e
    return found


def complete(workdir: str) -> bool:
    done = None
    for e in read(path(workdir)):
        if e.get("kind") == "session":
            done = False
        elif e.get("kind") == "result" and done is False:
            done = True
    return done is True
