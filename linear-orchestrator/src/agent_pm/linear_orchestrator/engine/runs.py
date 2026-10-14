"""The runs log (runs.jsonl): one durable JSON line per run event; its readers: start and resume
lines as Entries, live limit rows (`limits`), `trim` to KEEP.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from agent_pm.utils import append_line, save, timestamp

KEEP = timedelta(days=7)
ERRORS = (ValueError, TypeError, KeyError, OverflowError)


class Entry(NamedTuple):
    ts: datetime
    kind: str
    issue: str
    sid: str
    role: str
    runtime: str
    delegate_kind: str = "harness"


def note(path, kind, *, now=datetime.now, **fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps({"ts": timestamp(now=now), "kind": kind, **fields}, ensure_ascii=False)
    append_line(path, line + "\n", durable=True)


def note_limit(path, *, issue, sid, runtime, resets_at, delegate_kind="harness"):
    note(
        path,
        "limit",
        issue=issue,
        sid=sid,
        runtime=runtime,
        resets_at=resets_at,
        delegate_kind=delegate_kind,
    )


def _runtime(d):
    return isinstance(d["runtime"], str) and d["runtime"] != ""


def _delegate(d):
    return (
        isinstance(d.get("delegate_kind", "harness"), str)
        and d.get("delegate_kind", "harness") != ""
    )


def parse_line(line):
    try:
        d = json.loads(line)
        if (
            d["kind"] in ("start", "resume")
            and all(isinstance(d[k], str) for k in ("issue", "sid", "role"))
            and _runtime(d)
            and _delegate(d)
        ):
            ts = datetime.fromisoformat(d["ts"]).astimezone(UTC)
            return Entry(
                ts,
                d["kind"],
                d["issue"],
                d["sid"],
                d["role"],
                d["runtime"],
                d.get("delegate_kind", "harness"),
            )
    except ERRORS:
        pass
    return None


def _limit(line, now):
    try:
        d = json.loads(line)
        if (
            d["kind"] == "limit"
            and _runtime(d)
            and _delegate(d)
            and isinstance(d["resets_at"], str)
        ):
            at = datetime.fromisoformat(d["resets_at"])
            if at.utcoffset() is not None and now < at <= now + KEEP:
                return (d.get("delegate_kind", "harness"), d["runtime"]), at
    except ERRORS:
        pass
    return None


def _lines(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.readlines()
    except FileNotFoundError:
        return []


def parse_log(path):
    return [e for line in _lines(path) if (e := parse_line(line))]


def limits(path, now):
    found = {}
    for key, at in filter(None, (_limit(line, now) for line in _lines(path))):
        if key not in found or at > found[key]:
            found[key] = at
    return found


def latest(entries):
    found = {}
    for entry in entries:
        if entry.issue not in found or entry.ts >= found[entry.issue].ts:
            found[entry.issue] = entry
    return found


def trim(path, now):
    lines = _lines(path)
    entries = [e for line in lines if (e := parse_line(line))]
    newest = latest(entries)
    keep = [
        line
        for line in lines
        if (e := parse_line(line))
        and (e.ts >= now - KEEP or e.sid == newest[e.issue].sid)
        or _limit(line, now)
    ]
    if len(keep) < len(lines):
        save(path, "".join(keep))


def runtime_of(entries, sid):
    mine = [e for e in entries if e.sid == sid]
    return next((e for e in mine if e.kind == "start"), min(mine, key=lambda e: e.ts)).runtime


def delegate_of(entries, sid):
    mine = [e for e in entries if e.sid == sid]
    return next((e for e in mine if e.kind == "start"), min(mine, key=lambda e: e.ts)).delegate_kind
