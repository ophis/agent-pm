"""Shared data fixtures for this topic."""

import contextlib
import os
import re
import signal
import stat

from agent_pm.manager.engine import roster
from agent_pm.session import SessionRow

SID = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"


SID2 = "1b2c3d4e-5f60-7890-abcd-ef0123456789"


WHEN = "2026-01-01T00:00:00+00:00"


MODULE = ["-P", "-m", "agent_pm.manager.engine.crew"]


PRE_MOVE = ["-P", "-m", "agent_pm.crew"]


NOT_REGULAR = "{}: not a regular file owned by you"


NOT_A_DIR = "not a directory owned by you"


NO_RUNTIME = "config: runtime: want a runtime name"


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


@contextlib.contextmanager
def within(seconds=10):
    def fire(*_):
        raise AssertionError("blocked")

    old = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def row(name, *, own=False):
    return SessionRow(name, None, None, "working", None, None, None, None, own)


CHILD = """
import sys
from agent_pm.manager.engine import roster
from agent_pm.manager.engine.types import Entry
for i in range(15):
    spec = Entry("worker", sid=None, cwd="/w", resume=["/p", "/x/crew.py"])
    roster.put(sys.argv[1], sys.argv[2] + str(i), roster.entry(spec))
"""


LEASE_CHILD = """
import sys
from types import SimpleNamespace
from agent_pm.manager.engine import roster
from agent_pm.session import SessionRow
d, own = sys.argv[1], sys.argv[2]
live = [SessionRow("held", None, None, "working", None, None, None, None, False)]
sys.stdin.readline()
try:
    with roster.locked(d) as r:
        roster.take(r, d, own, driver=SimpleNamespace(sessions=lambda match: live))
except roster.Held as e:
    print(e)
    sys.exit(3)
"""


def good(**over):
    value = dict(
        kind="worker",
        sid=SID,
        cwd="/w",
        resume=["/usr/bin/python3", "/x/crew.py"],
        note=None,
        opener=None,
        pane=None,
        split=None,
        split_from=None,
        tui=None,
        state="working",
        started=WHEN,
    )
    return {**value, **over}


KIND = "kind: {!r}: want worker, role or pipeline"


VALID = {
    "kind": ["worker", "role", "pipeline"],
    "sid": [SID, None],
    "cwd": ["/w", "/w with space/\xe9\u65e5", "/w\xa0\u200b"],
    "resume": [
        ["/usr/bin/python3", "/x/crew.py", "; rm -rf ~"],
        ["/p", "/x/drive.py"],
        ["/p", "/x/router.py", "a", "b"],
        ["/p", "/x/workers.py", "start"],
        ["/p", *MODULE],
        ["/p", *MODULE, "start", "w1", "--", "-P"],
    ],
    "note": [None, "", "x" * 500, "n\xe9\xa0\u200b"],
    "opener": [None, "mgr", "w0t0p0:5E1B-C0FFEE", "w0t0p0:", "x" * 200],
    "pane": [None, "%3", "5E1B-C0FFEE", "%x", "x" * 200],
    "split": [None, "right", "below"],
    "split_from": [None, "mgr"],
    "tui": [None, "mgr"],
    "state": ["working", "done", "blocked", "dead", "gone", "waiting", "finished"],
    "started": [WHEN, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00-05:00"],
}


RESUME_LIST = "a list of 2 or more printable strings"


RESUME_1 = (
    "-P -m agent_pm.manager.engine.crew or an absolute path named crew.py, drive.py or router.py"
)


NOTE = "null or a printable string of at most 500 characters"


CWD = "an absolute printable path"


ID = "null or a printable string of 1–200 characters without whitespace"


INVALID = [
    ("kind", "boss", KIND),
    ("kind", None, KIND),
    ("kind", ["worker"], KIND),
    ("sid", "; rm -rf ~", "sid: {!r}: want a session id or null"),
    ("sid", SID.upper(), "sid: {!r}: want a session id or null"),
    ("sid", SID + "\n", "sid: {!r}: want a session id or null"),
    ("sid", 7, "sid: {!r}: want a session id or null"),
    ("cwd", "rel/w", f"cwd: {{!r}}: want {CWD}"),
    ("cwd", "/w\nx", f"cwd: {{!r}}: want {CWD}"),
    ("cwd", "/w\x85", f"cwd: {{!r}}: want {CWD}"),
    ("cwd", "/w\u2028x", f"cwd: {{!r}}: want {CWD}"),
    ("cwd", "/w\u2029", f"cwd: {{!r}}: want {CWD}"),
    ("cwd", "/w\x00", f"cwd: {{!r}}: want {CWD}"),
    ("cwd", "/w\x7f", f"cwd: {{!r}}: want {CWD}"),
    ("cwd", None, f"cwd: {{!r}}: want {CWD}"),
    ("resume", ["/usr/bin/python3"], f"resume: {{!r}}: want {RESUME_LIST}"),
    ("resume", "x", f"resume: {{!r}}: want {RESUME_LIST}"),
    ("resume", ["/p", "/x/crew.py", "a\nb"], f"resume: {{!r}}: want {RESUME_LIST}"),
    ("resume", ["/p", "/x/crew.py", "a\u2028"], f"resume: {{!r}}: want {RESUME_LIST}"),
    ("resume", ["/p", "/x/crew.py", 3], f"resume: {{!r}}: want {RESUME_LIST}"),
    ("resume", ["python3", "/x/crew.py"], "resume[0]: 'python3': want an absolute path"),
    ("resume", ["/p", "crew.py"], f"resume[1]: 'crew.py': want {RESUME_1}"),
    ("resume", ["/p", "/x/other.py"], f"resume[1]: '/x/other.py': want {RESUME_1}"),
    ("resume", ["/p", "/x/crew.py/"], f"resume[1]: '/x/crew.py/': want {RESUME_1}"),
    ("resume", ["/p", "workers.py"], f"resume[1]: 'workers.py': want {RESUME_1}"),
    ("resume", ["/p", "-P", "-m"], f"resume[1]: '-P': want {RESUME_1}"),
    (
        "resume",
        ["/p", "-P", "-m", "agent_pm.manager.engine.roster"],
        f"resume[1]: '-P': want {RESUME_1}",
    ),
    ("resume", ["/p", "-m", "agent_pm.manager.engine.crew"], f"resume[1]: '-m': want {RESUME_1}"),
    ("note", "a\nb", f"note: {{!r}}: want {NOTE}"),
    ("note", "x" * 501, f"note: {{!r}}: want {NOTE}"),
    ("note", "a\x9fb", f"note: {{!r}}: want {NOTE}"),
    ("note", "a\u2028b", f"note: {{!r}}: want {NOTE}"),
    ("note", 5, f"note: {{!r}}: want {NOTE}"),
    *(
        (field, value, f"{field}: {{!r}}: want {ID}")
        for field in ("opener", "pane")
        for value in ("", "a b", "%3 ", "a\tb", "a\xa0b", "a\u2028b", "x" * 201, 5, ["%3"])
    ),
    ("split", "left", "split: {!r}: want right, below or null"),
    ("split", "", "split: {!r}: want right, below or null"),
    ("split_from", "a b", "split_from: {!r}: want a session name or null"),
    ("split_from", "", "split_from: {!r}: want a session name or null"),
    ("tui", "a;", "tui: {!r}: want a session name or null"),
    ("state", "idle", "state: {!r}: want working, done, blocked, dead, gone, waiting or finished"),
    ("state", None, "state: {!r}: want working, done, blocked, dead, gone, waiting or finished"),
    ("started", "2026-01-01T00:00:00", "started: {!r}: want an ISO 8601 time with a UTC offset"),
    ("started", "soon", "started: {!r}: want an ISO 8601 time with a UTC offset"),
    ("started", "", "started: {!r}: want an ISO 8601 time with a UTC offset"),
    ("started", 5, "started: {!r}: want an ISO 8601 time with a UTC offset"),
    ("started", WHEN + "\x00", "started: {!r}: want an ISO 8601 time with a UTC offset"),
]


DROP = object()


TOP = [
    ("not JSON", b"{", "invalid JSON"),
    ("not UTF-8", b"\xff\xfe\x00", "invalid JSON"),
    ("nested too deep", b"[" * 200000, "invalid JSON"),
    ("a list", b"[]", "want a JSON object"),
    ("a key missing", {"gen": DROP}, "keys: missing ['gen']"),
    ("a key extra", {"x": 1}, "keys: unexpected ['x']"),
    ("version 2", {"version": 2}, "version: 2: want 1"),
    ("version true", {"version": True}, "version: True: want 1"),
    ("version a string", {"version": "1"}, "version: '1': want 1"),
    ("version 1.0", {"version": 1.0}, "version: 1.0: want 1"),
    ("holder a string", {"holder": "s"}, "holder: want null or an object with session and since"),
    ("holder without since", {"holder": {"session": "s"}}, "holder: keys: missing ['since']"),
    (
        "holder with an extra key",
        {"holder": {"session": "s", "since": WHEN, "x": 1}},
        "holder: keys: unexpected ['x']",
    ),
    (
        "holder session",
        {"holder": {"session": "a b", "since": WHEN}},
        "holder.session: 'a b': want a session name",
    ),
    (
        "holder since naive",
        {"holder": {"session": "s", "since": "2026-01-01T00:00:00"}},
        "holder.since: '2026-01-01T00:00:00': want an ISO 8601 time with a UTC offset",
    ),
    ("cursor negative", {"cursor": -1}, "cursor: -1: want a non-negative integer"),
    ("cursor true", {"cursor": True}, "cursor: True: want a non-negative integer"),
    ("cursor float", {"cursor": 1.0}, "cursor: 1.0: want a non-negative integer"),
    ("gen a string", {"gen": "0"}, "gen: '0': want a non-negative integer"),
    ("gen null", {"gen": None}, "gen: None: want a non-negative integer"),
    ("entries a list", {"entries": []}, "entries: want an object"),
]


NOW = object()


ATTACH = "manager directory {} not attached: run crew.py attach first"


KEPT = object()


def kills(*sessions):
    return [step for s in sessions for step in (("close", s), ("sessions", re.escape(s)))]


WIDTH = 64


SPAN = roster.EVENTS_MAX // WIDTH


WRITER = b"12:00:00 w9 done\n"


def numbered(count, *, start=1, first=WIDTH):
    return b"".join(
        f"line {i}".ljust((first if i == start else WIDTH) - 1, ".").encode() + b"\n"
        for i in range(start, start + count)
    )
