"""The runs log (runs.jsonl): one durable JSON line per run event; its readers: start and resume
lines as Entries, a sid's start row as its Claim, live limit rows (`limits`), `trim` to KEEP.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from agent_pm.linear_orchestrator.engine.types import UUID_RE, Claim
from agent_pm.utils import append_line, repo_slug, save, timestamp

KEEP = timedelta(days=7)
ERRORS = (ValueError, TypeError, KeyError, OverflowError)


class Entry(NamedTuple):
    ts: datetime
    kind: str
    issue: str
    sid: str
    role: str
    client: str
    delegate_kind: str = "harness"


def note(path, kind, *, now=datetime.now, **fields):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps({"ts": timestamp(now=now), "kind": kind, **fields}, ensure_ascii=False)
    append_line(path, line + "\n", durable=True)


def note_limit(path, *, issue, sid, client, resets_at, delegate_kind):
    note(
        path,
        "limit",
        issue=issue,
        sid=sid,
        client=client,
        resets_at=resets_at,
        delegate_kind=delegate_kind,
    )


def _client(d):
    return isinstance(d["client"], str) and d["client"] != ""


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
            and _client(d)
            and _delegate(d)
        ):
            ts = datetime.fromisoformat(d["ts"]).astimezone(UTC)
            return Entry(
                ts,
                d["kind"],
                d["issue"],
                d["sid"],
                d["role"],
                d["client"],
                d.get("delegate_kind", "harness"),
            )
    except ERRORS:
        pass
    return None


def _text(value):
    return isinstance(value, str) and value != ""


CLAIMED = {
    "role": _text,
    "uuid": lambda v: isinstance(v, str) and UUID_RE.fullmatch(v) is not None,
    "project": lambda v: v is None or isinstance(v, str),
    "target": lambda v: v is None or repo_slug(v) is not None,
    "key": _text,
    "client": _text,
    "delegate_kind": _text,
}


def claim(path, issue, sid) -> Claim:
    """The (issue, sid) start row's Claim, never another sid's; raises ValueError."""
    for line in _lines(path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict) and (d.get("kind"), d.get("issue"), d.get("sid")) == (
            "start",
            issue,
            sid,
        ):
            for name, valid in CLAIMED.items():
                if name not in d or not valid(d[name]):
                    raise ValueError(f"{issue}: session {sid}'s start row {name}: {d.get(name)!r}")
            return Claim(
                issue,
                sid,
                d["role"],
                d["uuid"],
                d["project"],
                d["target"] and repo_slug(d["target"]),
                d["key"],
                d["client"],
                d["delegate_kind"],
            )
    raise ValueError(f"{issue}: no start row for session {sid}")


def _limit(line, now):
    try:
        d = json.loads(line)
        if d["kind"] == "limit" and _client(d) and _delegate(d) and isinstance(d["resets_at"], str):
            at = datetime.fromisoformat(d["resets_at"])
            if at.utcoffset() is not None and now < at <= now + KEEP:
                return (d.get("delegate_kind", "harness"), d["client"]), at
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


def client_of(entries, sid):
    mine = [e for e in entries if e.sid == sid]
    return next((e for e in mine if e.kind == "start"), min(mine, key=lambda e: e.ts)).client


def delegate_of(entries, sid):
    mine = [e for e in entries if e.sid == sid]
    return next((e for e in mine if e.kind == "start"), min(mine, key=lambda e: e.ts)).delegate_kind
