"""Roster record schema, field validation, and printable table values."""

import datetime
import os
import re
import sys
import unicodedata

from agent_pm.manager.engine.types import RosterError

NAME = re.compile(r"[A-Za-z0-9_-]+")

SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

VERSION = 1

KINDS = ("worker", "role", "pipeline")

STATES = ("working", "done", "blocked", "dead", "gone", "waiting", "finished")

SPLITS = ("right", "below")

SCRIPTS = ("crew.py", "drive.py", "router.py")


# Older persisted entries remain valid; recovery rewrites their launch command.
LEGACY = "workers.py"

LEGACY_MODULE = ("-P", "-m", "agent_pm.crew")

CREW_MODULE = ("-P", "-m", "agent_pm.manager.engine.crew")

TOP = ("version", "holder", "cursor", "gen", "entries")

FIELDS = (
    "kind",
    "sid",
    "cwd",
    "resume",
    "note",
    "opener",
    "pane",
    "split",
    "split_from",
    "tui",
    "state",
    "started",
)

NOTE_MAX = 500

ID_MAX = 200


def printable(text: str) -> bool:
    # so a printed field stays one table cell or one command
    return not any(unicodedata.category(c) == "Cc" or c in "\u2028\u2029" for c in text)


def _or(words) -> str:
    return f"{', '.join(words[:-1])} or {words[-1]}"


def matches(pattern: re.Pattern, value) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _text(value) -> bool:
    return isinstance(value, str) and printable(value)


def _id(value) -> bool:
    return value is None or (
        _text(value) and 0 < len(value) <= ID_MAX and not any(c.isspace() for c in value)
    )


_TIME = "an ISO 8601 time with a UTC offset"


def _time(value) -> bool:
    if not _text(value):
        return False
    try:
        return datetime.datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _want(field: str, value, want) -> str:
    return f"{field}: {value!r}: want {want}"


def _keys(value: dict, want: tuple) -> str | None:
    missing, extra = (
        sorted(set(want) - set(value), key=str),
        sorted(set(value) - set(want), key=str),
    )
    parts = [
        *([f"missing {missing!r}"] if missing else []),
        *([f"unexpected {extra!r}"] if extra else []),
    ]
    return f"keys: {', '.join(parts)}" if parts else None


_NAME_OR_NULL = (lambda v: v is None or matches(NAME, v), "a session name or null")
_ID = (_id, f"null or a printable string of 1–{ID_MAX} characters without whitespace")
_CHECKS = {
    "kind": (lambda v: v in KINDS, _or(KINDS)),
    "sid": (lambda v: v is None or matches(SID, v), "a session id or null"),
    "cwd": (lambda v: _text(v) and os.path.isabs(v), "an absolute printable path"),
    "note": (
        lambda v: v is None or _text(v) and len(v) <= NOTE_MAX,
        f"null or a printable string of at most {NOTE_MAX} characters",
    ),
    "opener": _ID,
    "pane": _ID,
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
    if tuple(resume[1:4]) in (CREW_MODULE, LEGACY_MODULE):
        return None
    if not (os.path.isabs(resume[1]) and os.path.basename(resume[1]) in (*SCRIPTS, LEGACY)):
        want = f"{' '.join(CREW_MODULE)} or an absolute path named {_or(SCRIPTS)}"
        return _want("resume[1]", resume[1], want)
    return None


def value_fault(value) -> str | None:
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


def entry_fault(name, value) -> str | None:
    if not matches(NAME, name):
        return f"name: want {NAME.pattern}"
    return value_fault(value)


def _holder_fault(holder) -> str | None:
    if holder is None:
        return None
    if not isinstance(holder, dict):
        return "holder: want null or an object with session and since"
    if bad := _keys(holder, ("session", "since")):
        return f"holder: {bad}"
    if not matches(NAME, holder["session"]):
        return _want("holder.session", holder["session"], "a session name")
    if not _time(holder["since"]):
        return _want("holder.since", holder["since"], _TIME)
    return None


def _top_fault(data) -> str | None:
    if not isinstance(data, dict):
        return "want a JSON object"
    if bad := _keys(data, TOP):
        return bad
    if type(data["version"]) is not int or data["version"] != VERSION:
        return _want("version", data["version"], VERSION)
    if bad := _holder_fault(data["holder"]):
        return bad
    for field in ("cursor", "gen"):
        if type(data[field]) is not int or data[field] < 0:
            return _want(field, data[field], "a non-negative integer")
    return None if isinstance(data["entries"], dict) else "entries: want an object"


def validated(data, path: str, *, strict: bool) -> dict:
    if bad := _top_fault(data):
        raise RosterError.schema_fault(path, bad)
    entries = {}
    for name, value in data["entries"].items():
        if bad := entry_fault(name, value):
            if strict:
                raise RosterError.schema_fault(path, f"entry {name!r}: {bad}")
            print(f"manager: roster.json: entry {name!r}: {bad}", file=sys.stderr)
        else:
            entries[name] = value
    return {**data, "entries": entries}
