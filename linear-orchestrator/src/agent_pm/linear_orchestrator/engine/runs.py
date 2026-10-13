"""The runs log (runs.jsonl): one durable JSON line per run event; its readers: start and resume
lines as Entries, live limit rows (`limits`), `trim` to KEEP."""

import json
import os
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from agent_pm.linear_orchestrator.linear import linear
from agent_pm.utils import append_line, save

KEEP = timedelta(days=7)
ERRORS = (ValueError, TypeError, KeyError, OverflowError)


class Entry(NamedTuple):
    ts: datetime
    kind: str
    issue: str
    sid: str
    role: str
    runtime: str


def note(path, kind, **fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps({"ts": linear.stamp(), "kind": kind, **fields}, ensure_ascii=False)
    append_line(path, line + "\n", durable=True)


def note_limit(path, *, issue, sid, runtime, resets_at):
    note(path, "limit", issue=issue, sid=sid, runtime=runtime, resets_at=resets_at)


def _runtime(d):
    return isinstance(d["runtime"], str) and d["runtime"] != ""


def parse_line(line):
    try:
        d = json.loads(line)
        if (
            d["kind"] in ("start", "resume")
            and all(isinstance(d[k], str) for k in ("issue", "sid", "role"))
            and _runtime(d)
        ):
            ts = datetime.fromisoformat(d["ts"]).astimezone(UTC)
            return Entry(ts, d["kind"], d["issue"], d["sid"], d["role"], d["runtime"])
    except ERRORS:
        pass
    return None


def _limit(line, now):
    try:
        d = json.loads(line)
        if d["kind"] == "limit" and _runtime(d) and isinstance(d["resets_at"], str):
            at = datetime.fromisoformat(d["resets_at"])
            if at.utcoffset() is not None and now < at <= now + KEEP:
                return d["runtime"], at
    except ERRORS:
        pass
    return None


def _lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def parse_log(path):
    return [e for line in _lines(path) if (e := parse_line(line))]


def limits(path, now):
    found = {}
    for runtime, at in filter(None, (_limit(line, now) for line in _lines(path))):
        if runtime not in found or at > found[runtime]:
            found[runtime] = at
    return found


def trim(path, now):
    lines = _lines(path)
    keep = [
        line
        for line in lines
        if (e := parse_line(line)) and e.ts >= now - KEEP or _limit(line, now)
    ]
    if len(keep) < len(lines):
        save(path, "".join(keep))


def runtime_of(entries, sid):
    mine = [e for e in entries if e.sid == sid]
    return next((e for e in mine if e.kind == "start"), min(mine, key=lambda e: e.ts)).runtime
