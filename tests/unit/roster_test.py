"""agent_pm.manager.roster, the old core's roster_test ported onto a fake session.engine.api (Tmux): LEGACY_CLIENT's cases became
FR-28's default runtime; worker_dir, default_runtime and the crew module's entries are new."""

import contextlib
import datetime
import errno
import fcntl
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tomllib
import types
import unittest
import unittest.mock

import agent_pm.delegate.engine.config
from agent_pm.manager import roster
from agent_pm.session import api

from tests.case import PY, SRC, Case, child_env

SID = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
SID2 = "1b2c3d4e-5f60-7890-abcd-ef0123456789"
WHEN = "2026-01-01T00:00:00+00:00"
MODULE = ["-P", "-m", "agent_pm.manager.crew"]
PRE_MOVE = ["-P", "-m", "agent_pm.crew"]
NOT_REGULAR = "{}: not a regular file owned by you"
NOT_A_DIR = "not a directory owned by you"
NO_RUNTIME = "core config: runtime: want a runtime name"


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


def row(name, own=False):
    return api.SessionRow(name, None, None, "working", None, None, None, None, own)


class Tmux:
    def __init__(self):
        self.alive, self.stuck, self.own, self.error, self.calls = set(), set(), None, None, []

    def sessions(self, match=None):
        self.calls.append(("sessions", match))
        if self.error:
            raise self.error
        names = sorted({*self.alive, *([self.own] if self.own else [])})
        return [row(n, n == self.own) for n in names if match is None or re.fullmatch(match, n)]

    def close(self, name):
        self.calls.append(("close", name))
        if self.error:
            raise self.error
        if name in self.stuck:
            raise api.DriverError(f"cannot close tui session {name}: tmux: busy")
        self.alive.discard(name)


class Base(Case):
    def setUp(self):
        self.managers = os.path.join(self.agent_pm, "managers")
        self.dir = os.path.join(self.managers, "m1")
        self.tmux = Tmux()
        for name in ("sessions", "close"):
            self.enterContext(unittest.mock.patch.object(api, name, getattr(self.tmux, name)))

    def umask(self, mask):
        self.addCleanup(os.umask, os.umask(mask))

    def outside(self, name):
        path = os.path.join(os.path.dirname(self.agent_pm), name)
        os.mkdir(path)
        return path

    def local(self, text):
        with open(os.path.join(self.agent_pm, "core-new.local.toml"), "w") as f:
            f.write(text)


class ManagerTest(Base):
    def test_directory_is_the_manager_else_the_own_session_and_touches_nothing(self):
        asked = [("sessions", None)]
        cases = [
            ("--manager beats the own session", "m1", "own", self.dir, []),
            ("the own session", None, "own", os.path.join(self.managers, "own"), asked),
            ("no own row (outside tmux): none", None, None, None, asked),
        ]
        for label, name, own, want, calls in cases:
            with self.subTest(label):
                self.tmux.alive, self.tmux.own, self.tmux.calls = {"other"}, own, []
                self.assertEqual(roster.directory(name), want)
                self.assertEqual(self.tmux.calls, calls)
                self.assertEqual(os.listdir(self.agent_pm), [])

    def test_relative_home_gives_an_absolute_path(self):
        os.environ["HOME"] = "rel/home"
        want = os.path.join(os.path.abspath("rel/home"), ".agent-pm", "managers", "m1")
        self.assertEqual(roster.directory("m1"), want)

    def test_bad_manager_names(self):
        for name in ("a/b", "..", "", "a b", "a;", "a\n"):
            for call, resolve in (
                ("directory", roster.directory),
                ("home", lambda n: roster.home(n, None)),
            ):
                with (
                    self.subTest(name=name, call=call),
                    self.assertRaises(roster.RosterError) as cm,
                ):
                    resolve(name)
                self.assertEqual(str(cm.exception), f"manager {name!r}: want [A-Za-z0-9_-]+")
        self.assertEqual(self.tmux.calls, [])

    def test_unknown_sessions_name_the_manager_option(self):
        self.tmux.error = api.DriverError("tmux sessions unknown: bad $TMUX_PANE '%3;'")
        for call, resolve in (
            ("directory", roster.directory),
            ("home", lambda: roster.home(None, None)),
        ):
            with self.subTest(call), self.assertRaises(roster.RosterError) as cm:
                resolve()
            self.assertEqual(
                str(cm.exception),
                "tmux sessions unknown: bad $TMUX_PANE '%3;'; give --manager <name>",
            )

    def test_home_is_the_directory_whose_events_file_is_given(self):
        own = os.path.join(self.managers, "own")
        self.tmux.own = "own"
        for label, name, d, calls in (
            ("--manager", "m1", self.dir, 0),
            ("the own session", None, own, 1),
        ):
            with self.subTest(label):
                roster.ensure(d)
                self.tmux.calls = []
                self.assertEqual(roster.home(name, os.path.join(d, "events")), d)
                self.assertEqual(len(self.tmux.calls), calls)
                self.assertEqual(os.listdir(d), [])

    def test_home_is_none_without_the_directory_or_its_events_file(self):
        roster.ensure(self.dir)
        events = os.path.join(self.dir, "events")
        cases = {
            "events another file": ("m1", os.path.join(self.outside("x"), "events")),
            "events a sibling file": ("m1", os.path.join(self.dir, "roster.json")),
            "events None": ("m1", None),
            "directory missing": ("m2", os.path.join(self.managers, "m2", "events")),
            "no own row, no name": (None, events),
        }
        for why, (name, given) in cases.items():
            with self.subTest(why):
                self.assertIsNone(roster.home(name, given))
        self.assertEqual(os.listdir(self.managers), ["m1"])
        self.assertEqual(os.listdir(self.dir), [])

    def test_home_compares_real_paths(self):
        roster.ensure(self.dir)
        base = os.path.dirname(self.agent_pm)
        os.symlink(self.dir, os.path.join(base, "link"))
        self.assertEqual(roster.home("m1", os.path.join(base, "link", "events")), self.dir)
        os.symlink(base, os.path.join(base, "alias"))
        os.environ["HOME"] = os.path.join(base, "alias")
        self.assertEqual(
            roster.home("m1", os.path.join(self.dir, "events")),
            os.path.join(base, "alias", ".agent-pm", "managers", "m1"),
        )

    def test_events_path_without_create_touches_nothing(self):
        self.assertEqual(roster.events(self.dir, create=False), os.path.join(self.dir, "events"))
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_created_0700_with_a_0600_events_whatever_the_umask(self):
        for mask in (0o022, 0o077):
            with self.subTest(umask=oct(mask)):
                shutil.rmtree(self.agent_pm)
                self.umask(mask)
                path = roster.events(self.dir)
                self.assertEqual(path, os.path.join(self.dir, "events"))
                for made in (self.agent_pm, self.managers, self.dir):
                    self.assertEqual(mode(made), 0o700, made)
                self.assertTrue(stat.S_ISREG(os.lstat(path).st_mode))
                self.assertEqual(mode(path), 0o600)

    def test_directories_are_0700_even_when_the_umask_strips_owner_bits(self):
        self.umask(0o277)
        roster.ensure(self.dir)
        self.assertEqual([mode(self.managers), mode(self.dir)], [0o700, 0o700])

    def test_existing_managers_directory_keeps_its_mode(self):
        os.mkdir(self.managers)
        os.chmod(self.managers, 0o755)
        roster.ensure(self.dir)
        self.assertEqual([mode(self.managers), mode(self.dir)], [0o755, 0o700])

    def test_existing_directory_is_kept(self):
        roster.ensure(self.dir)
        with open(roster.events(self.dir), "w") as f:
            f.write("12:00:00 w1 done\n")
        roster.ensure(self.dir)
        roster.events(self.dir)
        self.assertEqual(mode(self.dir), 0o700)
        with open(os.path.join(self.dir, "events")) as f:
            self.assertEqual(f.read(), "12:00:00 w1 done\n")

    def test_directory_open_to_group_or_other_refused_and_not_chmodded(self):
        roster.ensure(self.dir)
        for bits in (0o750, 0o705, 0o770, 0o777, 0o701):
            os.chmod(self.dir, bits)
            for call in (roster.ensure, roster.events):
                with (
                    self.subTest(bits=oct(bits), call=call.__name__),
                    self.assertRaisesRegex(
                        roster.RosterError, f"^manager directory {self.dir}: .*group or other"
                    ),
                ):
                    call(self.dir)
            self.assertEqual(mode(self.dir), bits)
            self.assertEqual(os.listdir(self.dir), [])

    def refused(self, path):
        with self.assertRaises(roster.RosterError) as cm:
            roster.events(self.dir)
        self.assertEqual(str(cm.exception), f"manager directory {path}: {NOT_A_DIR}")

    def test_managers_symlink_refused_and_its_target_untouched(self):
        target = self.outside("target")
        os.symlink(target, self.managers)
        self.refused(self.managers)
        self.assertEqual(os.listdir(target), [])

    def test_name_symlink_refused_and_its_target_untouched(self):
        target = self.outside("target")
        os.mkdir(self.managers)
        os.symlink(target, self.dir)
        self.refused(self.dir)
        self.assertEqual(os.listdir(target), [])

    def test_name_file_refused(self):
        os.mkdir(self.managers)
        open(self.dir, "w").close()
        self.refused(self.dir)

    def test_directory_owned_by_another_user_refused(self):
        os.mkdir(self.managers)
        with unittest.mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.refused(self.managers)

    def test_agent_pm_a_file_refused(self):
        os.rmdir(self.agent_pm)
        open(self.agent_pm, "w").close()
        with self.assertRaisesRegex(roster.RosterError, f"^manager directory {self.agent_pm}: "):
            roster.ensure(self.dir)

    def test_events_not_a_regular_file_of_yours_refused_without_blocking(self):
        events = os.path.join(self.dir, "events")
        target = os.path.join(self.outside("target"), "file")
        real = os.fstat

        def foreign(fd):
            st = real(fd)
            return types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1)

        cases = [
            ("a symlink", lambda: os.symlink(target, events), None, NOT_REGULAR.format(events)),
            ("a fifo", lambda: os.mkfifo(events), None, None),
            ("a directory", lambda: os.mkdir(events), None, None),
            (
                "another user's",
                lambda: open(events, "w").close(),
                foreign,
                NOT_REGULAR.format(events),
            ),
        ]
        for label, make, fstat, message in cases:
            with self.subTest(label):
                roster.ensure(self.dir)
                with open(target, "w"):
                    pass
                make()
                with (
                    unittest.mock.patch("os.fstat", fstat) if fstat else contextlib.nullcontext(),
                    within(),
                    self.assertRaises(roster.RosterError) as cm,
                ):
                    roster.events(self.dir)
                if message is None:
                    self.assertRegex(str(cm.exception), f"^{re.escape(events)}: ")
                else:
                    self.assertEqual(str(cm.exception), message)
                self.assertEqual(os.path.getsize(target), 0)
                shutil.rmtree(self.dir)


CHILD = """
import sys
from agent_pm.manager import roster
for i in range(15):
    roster.put(sys.argv[1], sys.argv[2] + str(i), roster.entry("worker", sid=None, cwd="/w", resume=["/p", "/x/crew.py"]))
"""

LEASE_CHILD = """
import sys
from unittest import mock
from agent_pm.manager import roster
from agent_pm.session import api
d, own = sys.argv[1], sys.argv[2]
live = [api.SessionRow("held", None, None, "working", None, None, None, None, False)]
sys.stdin.readline()
try:
    with mock.patch.object(api, "sessions", return_value=live), roster.locked(d) as r:
        roster.take(r, d, own)
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
RESUME_1 = "-P -m agent_pm.manager.crew or an absolute path named crew.py, drive.py or router.py"
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
    ("resume", ["/p", "-P", "-m", "agent_pm.manager.roster"], f"resume[1]: '-P': want {RESUME_1}"),
    ("resume", ["/p", "-m", "agent_pm.manager.crew"], f"resume[1]: '-m': want {RESUME_1}"),
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


class RosterTest(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, "roster.json")
        self.lock = os.path.join(self.dir, "roster.lock")
        roster.ensure(self.dir)

    def doc(self, **top):
        return {"version": 1, "holder": None, "cursor": 0, "gen": 0, "entries": {}, **top}

    def store(self, data):
        raw = (
            data
            if isinstance(data, bytes)
            else data.encode()
            if isinstance(data, str)
            else json.dumps(data).encode()
        )
        with open(self.path, "wb") as f:
            f.write(raw)
        os.chmod(self.path, 0o600)
        return raw

    def bytes(self):
        with open(self.path, "rb") as f:
            return f.read()

    def entries(self):
        return json.loads(self.bytes())["entries"]

    def load(self, **kw):
        err = io.StringIO()
        with (
            contextlib.redirect_stderr(err),
            roster.locked(self.dir, **{"write": False, **kw}) as r,
        ):
            pass
        return r, err.getvalue()

    def refused(self, message, **kw):
        with self.assertRaises(roster.RosterError) as cm, roster.locked(self.dir, **kw):
            pass
        self.assertEqual(str(cm.exception), message)

    def hold(self):
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)

    def foreign(self, path):
        ino, real = os.stat(path).st_ino, os.fstat

        def fstat(fd):
            st = real(fd)
            if st.st_ino != ino:
                return st
            return types.SimpleNamespace(
                st_mode=st.st_mode, st_uid=st.st_uid + 1, st_size=st.st_size
            )

        return unittest.mock.patch("os.fstat", fstat)

    def test_fresh_roster_is_written_0600_with_the_lock(self):
        self.addCleanup(os.umask, os.umask(0))
        with roster.locked(self.dir) as r:
            self.assertEqual(r, self.doc())
        with open(self.path) as f:
            self.assertEqual(f.read(), json.dumps(self.doc(), indent=2, sort_keys=True) + "\n")
        self.assertEqual([mode(self.path), mode(self.lock), mode(self.dir)], [0o600, 0o600, 0o700])
        self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])
        self.assertEqual(self.load()[0], self.doc())

    def test_changes_are_written_and_read_back(self):
        holder = {"session": "s1", "since": WHEN}
        with roster.locked(self.dir) as r:
            r.update(holder=holder, cursor=4, gen=2)
            r["entries"]["w1"] = good(note="n")
        r, err = self.load()
        self.assertEqual(
            r, self.doc(holder=holder, cursor=4, gen=2, entries={"w1": good(note="n")})
        )
        self.assertEqual(err, "")
        self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])

    def test_unchanged_roster_is_not_rewritten(self):
        raw = self.store(json.dumps(self.doc(entries={"w1": good()}), separators=(",", ":")))
        with roster.locked(self.dir):
            pass
        self.assertEqual(self.bytes(), raw)

    def test_write_false_or_an_exception_in_the_block_writes_nothing_even_when_the_file_is_missing(
        self,
    ):
        def write_false():
            with roster.locked(self.dir, write=False) as r:
                r["cursor"] = 9

        def raising():
            with self.assertRaises(RuntimeError), roster.locked(self.dir) as r:
                r["cursor"] = 9
                raise RuntimeError

        for label, block in (("write=False", write_false), ("an exception", raising)):
            with self.subTest(label):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
                block()
                self.assertFalse(os.path.exists(self.path))
                raw = self.store(self.doc())
                block()
                self.assertEqual(self.bytes(), raw)

    def test_lock_is_released_after_the_block(self):
        with self.assertRaises(RuntimeError), roster.locked(self.dir):
            raise RuntimeError
        fd = os.open(self.lock, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_write_back_validates_the_whole_dict(self):
        raw = self.store(self.doc(entries={"w1": good()}))
        bad = [
            (lambda r: r.update(cursor=-1), "cursor: -1: want a non-negative integer"),
            (lambda r: r.update(x=1), "keys: unexpected ['x']"),
            (
                lambda r: r["entries"].update(w2=good(kind="boss")),
                "entry 'w2': " + KIND.format("boss"),
            ),
            (
                lambda r: r["entries"].update({"a b": good()}),
                "entry 'a b': name: want [A-Za-z0-9_-]+",
            ),
            (
                lambda r: r["entries"].update(w2=good(resume=("/p", "/x/crew.py"))),
                f"entry 'w2': resume: ('/p', '/x/crew.py'): want {RESUME_LIST}",
            ),
            (
                lambda r: r["entries"].update(w2={}),
                "entry 'w2': keys: missing " + str(sorted(good())),
            ),
        ]
        for change, why in bad:
            with self.subTest(why=why), self.assertRaises(roster.RosterError) as cm:
                with roster.locked(self.dir) as r:
                    change(r)
            self.assertEqual(str(cm.exception), f"{self.path}: {why}")
            self.assertEqual(self.bytes(), raw)
            self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])

    def test_failed_write_removes_its_temp_file_and_keeps_the_old_file(self):
        raw = self.store(self.doc())
        with unittest.mock.patch("os.replace", side_effect=OSError(5, "Input/output error")):
            with self.assertRaises(roster.RosterError) as cm, roster.locked(self.dir) as r:
                r["cursor"] = 1
        self.assertEqual(str(cm.exception), f"{self.path}: Input/output error")
        self.assertEqual(self.bytes(), raw)
        self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])

    def test_valid_entries_are_kept(self):
        for field, values in VALID.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.store(self.doc(entries={"w1": good(**{field: value})}))
                    r, err = self.load()
                    self.assertEqual(r["entries"], {"w1": good(**{field: value})})
                    self.assertEqual(err, "")

    def test_invalid_entry_is_skipped_with_one_stderr_line_and_the_rest_kept(self):
        for field, value, why in INVALID:
            with self.subTest(field=field, value=value):
                raw = self.store(self.doc(entries={"ok": good(), "bad": good(**{field: value})}))
                r, err = self.load()
                self.assertEqual(r["entries"], {"ok": good()})
                self.assertEqual(err, f"manager: roster.json: entry 'bad': {why.format(value)}\n")
                self.assertEqual(self.bytes(), raw)

    def test_entry_of_other_keys_than_the_twelve_or_a_bad_name_is_skipped(self):
        missing = {k: v for k, v in good().items() if k != "note"}
        cases = [
            ("bad", {**good(), "x": 1}, "keys: unexpected ['x']"),
            ("bad", missing, "keys: missing ['note']"),
            ("bad", {**missing, "y": 1}, "keys: missing ['note'], unexpected ['y']"),
            ("bad", [], "value: []: want an object"),
            ("bad", "w", "value: 'w': want an object"),
            *(
                (name, good(), "name: want [A-Za-z0-9_-]+")
                for name in ("a b", "", "a;", "a\nb", "w/1")
            ),
        ]
        for name, value, why in cases:
            with self.subTest(name=name, why=why):
                self.store(self.doc(entries={name: value}))
                r, err = self.load()
                self.assertEqual(
                    (r["entries"], err), ({}, f"manager: roster.json: entry {name!r}: {why}\n")
                )

    def test_stderr_line_shows_values_by_repr(self):
        self.store(self.doc(entries={"bad": good(cwd="/w\n\x1b[31mforged\u2028")}))
        err = self.load()[1]
        self.assertEqual(err.count("\n"), 1)
        self.assertNotIn("\x1b", err)
        self.assertIn(repr("/w\n\x1b[31mforged\u2028"), err)

    def test_later_write_drops_a_skipped_entry(self):
        self.store(self.doc(entries={"bad": good(sid="; rm -rf ~")}))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            roster.put(self.dir, "w1", good())
        self.assertEqual(list(self.entries()), ["w1"])
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_top_level_valid_forms(self):
        for top in (
            {},
            {"holder": {"session": "s1", "since": WHEN}},
            {"cursor": 7, "gen": 3},
            {"holder": {"session": "s1", "since": "2026-01-01T00:00:00Z"}},
        ):
            with self.subTest(top=top):
                self.store(self.doc(**top))
                self.assertEqual(self.load()[0], self.doc(**top))

    def test_top_level_faults_refuse_and_leave_the_bytes(self):
        for label, data, why in TOP:
            with self.subTest(label):
                top = (
                    data
                    if isinstance(data, bytes)
                    else {k: v for k, v in self.doc(**data).items() if v is not DROP}
                )
                raw = self.store(top)
                for kw in ({}, {"write": False}):
                    with self.assertRaises(roster.RosterError) as cm, roster.locked(self.dir, **kw):
                        self.fail("entered")
                    self.assertTrue(
                        str(cm.exception).startswith(f"{self.path}: {why}"), str(cm.exception)
                    )
                    self.assertEqual(self.bytes(), raw)
                self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])

    def test_roster_json_symlink_refused_and_its_target_untouched(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        with open(target, "w") as f:
            json.dump(self.doc(), f)
        os.symlink(target, self.path)
        self.refused(NOT_REGULAR.format(self.path))
        self.refused(NOT_REGULAR.format(self.path), write=False)
        with open(target) as f:
            self.assertEqual(json.load(f), self.doc())
        self.assertTrue(os.path.islink(self.path))

    def test_roster_json_fifo_refused_without_blocking(self):
        os.mkfifo(self.path)
        with within():
            self.refused(NOT_REGULAR.format(self.path))
            self.refused(NOT_REGULAR.format(self.path), write=False)
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.path).st_mode))

    def test_roster_json_directory_refused(self):
        os.mkdir(self.path)
        self.refused(NOT_REGULAR.format(self.path))

    def test_roster_json_of_another_user_refused(self):
        raw = self.store(self.doc())
        with self.foreign(self.path):
            self.refused(NOT_REGULAR.format(self.path))
        self.assertEqual(self.bytes(), raw)

    def test_oversize_roster_json_refused_and_the_limit_itself_read(self):
        raw = self.store(b"x" * (roster.ROSTER_MAX + 1))
        self.refused(f"{self.path}: over {roster.ROSTER_MAX} bytes")
        self.assertEqual(self.bytes(), raw)
        body = json.dumps(self.doc()).encode()
        self.store(body + b" " * (roster.ROSTER_MAX - len(body)))
        self.assertEqual(os.path.getsize(self.path), roster.ROSTER_MAX)
        self.assertEqual(self.load()[0], self.doc())

    def test_roster_lock_symlink_refused_and_its_target_untouched(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        open(target, "w").close()
        os.symlink(target, self.lock)
        self.refused(NOT_REGULAR.format(self.lock))
        self.assertEqual(os.path.getsize(target), 0)
        self.assertFalse(os.path.exists(self.path))

    def test_roster_lock_of_another_user_refused(self):
        open(self.lock, "w").close()
        with self.foreign(self.lock):
            self.refused(NOT_REGULAR.format(self.lock))
        self.assertFalse(os.path.exists(self.path))

    def test_busy_lock_is_refused_after_the_timeout(self):
        self.hold()
        clock = types.SimpleNamespace(now=0.0, slept=[])

        def sleep(seconds):
            clock.slept.append(seconds)
            clock.now += seconds

        with (
            unittest.mock.patch("time.monotonic", lambda: clock.now),
            unittest.mock.patch("time.sleep", sleep),
        ):
            self.refused(f"{self.lock}: busy")
        self.assertEqual(set(clock.slept), {0.1})
        self.assertAlmostEqual(sum(clock.slept), roster.LOCK_TIMEOUT, delta=0.2)
        self.assertFalse(os.path.exists(self.path))

    def test_two_processes_put_concurrently_and_both_entries_land(self):
        procs = [
            subprocess.Popen(
                [*PY, "-c", CHILD, self.dir, tag],
                env=child_env(),
                stderr=subprocess.PIPE,
                text=True,
            )
            for tag in "ab"
        ]
        for p in procs:
            _, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual(set(self.entries()), {f"{t}{i}" for t in "ab" for i in range(15)})

    def test_missing_directory_is_not_attached_and_nothing_is_created(self):
        os.rmdir(self.dir)
        message = f"manager directory {self.dir} not attached: run crew.py attach first"
        self.refused(message)
        self.refused(message, write=False)
        self.assertEqual(os.listdir(self.managers), [])
        os.rmdir(self.managers)
        self.refused(message)
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_directory_open_to_group_or_other_refused_not_chmodded_and_nothing_created(self):
        os.chmod(self.dir, 0o750)
        with (
            self.assertRaisesRegex(
                roster.RosterError, f"^manager directory {self.dir}: .*group or other"
            ),
            roster.locked(self.dir),
        ):
            pass
        self.assertEqual(mode(self.dir), 0o750)
        self.assertEqual(os.listdir(self.dir), [])

    def test_directory_symlink_file_and_foreign_owner_refused(self):
        refused = f"manager directory {self.dir}: {NOT_A_DIR}"
        real = os.lstat

        def lstat(path, *args, **kw):
            st = real(path, *args, **kw)
            return (
                types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1)
                if path == self.dir
                else st
            )

        with unittest.mock.patch("os.lstat", lstat):
            self.refused(refused)
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        os.mkdir(target, 0o700)
        os.rmdir(self.dir)
        os.symlink(target, self.dir)
        self.refused(refused)
        self.assertEqual(os.listdir(target), [])
        os.unlink(self.dir)
        open(self.dir, "w").close()
        self.refused(refused)

    def test_managers_of_another_user_or_a_symlink_refused(self):
        refused = f"manager directory {self.managers}: {NOT_A_DIR}"
        with unittest.mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.refused(refused)
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        os.rename(self.managers, target)
        os.symlink(target, self.managers)
        self.refused(refused)
        self.assertEqual(os.listdir(os.path.join(target, "m1")), [])

    def test_entry_defaults_and_validity(self):
        e = roster.entry("worker", sid=SID, cwd="/w", resume=good()["resume"])
        self.assertEqual(sorted(e), sorted(good()))
        self.assertEqual({**e, "started": WHEN}, good())
        self.assertEqual(e["state"], "working")
        self.assertIsNotNone(datetime.datetime.fromisoformat(e["started"]).tzinfo)
        full = roster.entry(
            "role",
            sid=None,
            cwd="/w",
            resume=["/p", "/x/drive.py"],
            note="n",
            opener="mgr",
            pane="%3",
            split="right",
            split_from="a",
            tui="t",
            state="done",
            started=WHEN,
        )
        self.assertEqual(
            full,
            good(
                kind="role",
                sid=None,
                resume=["/p", "/x/drive.py"],
                note="n",
                opener="mgr",
                pane="%3",
                split="right",
                split_from="a",
                tui="t",
                state="done",
            ),
        )
        with self.assertRaises(roster.RosterError) as cm:
            roster.entry("boss", sid=SID, cwd="/w", resume=["/p", "/x/crew.py"])
        self.assertEqual(str(cm.exception), "entry: " + KIND.format("boss"))
        with self.assertRaisesRegex(roster.RosterError, "^entry: cwd: 'rel': want"):
            roster.entry("worker", sid=SID, cwd="rel", resume=["/p", "/x/crew.py"])

    def test_set_entry_and_record_validate_replace_and_leave_the_dict_on_a_fault(self):
        for setter in (roster.set_entry, roster.record):
            with self.subTest(setter.__name__):
                with roster.locked(self.dir) as r:
                    r["entries"] = {}
                    setter(r, "w1", good())
                    setter(r, "w1", good(note="n"))
                    with self.assertRaises(roster.RosterError) as cm:
                        setter(r, "w2", good(sid="x"))
                    self.assertEqual(
                        str(cm.exception), "entry 'w2': sid: 'x': want a session id or null"
                    )
                    with self.assertRaisesRegex(roster.RosterError, "^entry 'w1': sid: 'x': want"):
                        setter(r, "w1", good(sid="x"))
                    with self.assertRaisesRegex(roster.RosterError, "^entry 'a b': name: want"):
                        setter(r, "a b", good())
                self.assertEqual(self.entries(), {"w1": good(note="n")})

    def test_put_records_one_entry_under_the_lock(self):
        roster.put(self.dir, "w1", good(note="n"))
        roster.put(self.dir, "w2", good(state="done"))
        roster.put(self.dir, "w1", good(state="done"))
        self.assertEqual(
            self.entries(), {"w1": good(state="done", note="n"), "w2": good(state="done")}
        )
        with self.assertRaises(roster.RosterError):
            roster.put(self.dir, "w3", good(kind="boss"))
        self.assertEqual(sorted(self.entries()), ["w1", "w2"])

    def test_record_keeps_the_note_of_a_replaced_entry_of_the_same_sid(self):
        cases = {
            "same sid, note None": (
                good(note="n"),
                good(state="done"),
                good(state="done", note="n"),
            ),
            "new sid": (good(note="n"), good(sid=SID2), good(sid=SID2)),
            "both sids null": (good(sid=None, note="n"), good(sid=None), good(sid=None)),
            "a given note wins": (good(note="n"), good(note="m"), good(note="m")),
            "no old entry": (None, good(), good()),
        }
        for why, (old, value, want) in cases.items():
            with self.subTest(why):
                given = dict(value)
                with roster.locked(self.dir) as r:
                    r["entries"] = {} if old is None else {"w1": old}
                    roster.record(r, "w1", value)
                self.assertEqual(self.entries(), {"w1": want})
                self.assertEqual(value, given)

    def test_unwritten_prints_the_one_failure_line_to_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            roster.unwritten("w1", roster.RosterError("/d/roster.lock: busy"))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "manager: entry w1 not written: /d/roster.lock: busy\n")

    def test_remove_returns_the_entry_or_refuses_a_missing_one(self):
        with roster.locked(self.dir) as r:
            roster.set_entry(r, "w1", good())
        with roster.locked(self.dir) as r:
            self.assertEqual(roster.remove(r, "w1"), good())
            with self.assertRaises(roster.RosterError) as cm:
                roster.remove(r, "w1")
            self.assertEqual(str(cm.exception), "w1 not in roster")
        self.assertEqual(self.entries(), {})

    def test_recovery_forms_each_a_copy(self):
        resume = ["/usr/bin/python3", *MODULE, "start"]
        cases = [
            ("worker", SID, resume),
            ("pipeline", SID, resume),
            ("worker", None, resume),
            (
                "role",
                SID,
                [*resume, "--sid", SID, "--resume", "--input", "Continue the unfinished task."],
            ),
            ("role", None, None),
        ]
        for kind, sid, want in cases:
            with self.subTest(kind=kind, sid=sid):
                e = good(kind=kind, sid=sid, resume=list(resume))
                got = roster.recovery(e)
                self.assertEqual(got, want)
                if got is not None:
                    got.append("x")
                    self.assertEqual(e["resume"], resume)

    def test_recovery_runs_the_crew_module_for_a_crew_py_or_legacy_workers_py_entry(self):
        self.assertEqual(
            roster.CREW,
            os.path.realpath(os.path.join(SRC["manager"], "agent_pm", "manager", "crew.py")),
        )
        self.assertTrue(os.path.isfile(roster.CREW))
        for script in ("/old/version/workers.py", "/old/skills/tmux/scripts/crew.py"):
            resume = ["/usr/bin/python3", script, "start", "w1"]
            for kind in ("worker", "pipeline", "role"):
                with self.subTest(script=script, kind=kind):
                    e = good(kind=kind, resume=list(resume))
                    got = roster.recovery(e)
                    self.assertEqual(got[:6], ["/usr/bin/python3", *MODULE, "start", "w1"])
                    self.assertEqual(e["resume"], resume)

    def test_a_pre_move_crew_module_entry_is_kept_and_recovers_to_the_crew_module(self):
        self.local('runtime = "rt9"\n')
        resume = ["/usr/bin/python3", *PRE_MOVE, "start", "w1"]
        self.store(self.doc(entries={"w1": good(resume=resume)}))
        r, err = self.load()
        self.assertEqual((r["entries"], err), ({"w1": good(resume=resume)}, ""))
        self.assertEqual(
            roster.recovery(r["entries"]["w1"]),
            ["/usr/bin/python3", *MODULE, "start", "w1", "--client", "rt9"],
        )
        roster.put(self.dir, "w2", good())
        self.assertEqual(self.entries(), {"w1": good(resume=resume), "w2": good()})

    def test_recovery_gives_a_worker_without_a_client_the_default_runtime(self):
        self.local('runtime = "rt9"\n')
        head = ["/usr/bin/python3", *MODULE, "start", "w1"]
        legacy = ["/usr/bin/python3", "/x/crew.py", "start", "w1"]
        script = ["/usr/bin/python3", "/x/drive.py", "start", "w1"]
        rest = ["--manager", "m", "--resume", SID, "--cwd", "/w"]
        default = [*head, "--client", "rt9", *rest]
        cases = [
            ("worker", [*head, *rest], default),
            ("worker", [*legacy, *rest], default),
            ("worker", [*script, *rest], [*script, "--client", "rt9", *rest]),
            ("worker", [*head, *rest, "--", "--client", "x"], [*default, "--", "--client", "x"]),
            ("worker", [*head, "--client", "other", *rest], [*head, "--client", "other", *rest]),
            ("pipeline", [*script, *rest], [*script, *rest]),
            (
                "role",
                [*script, *rest],
                [
                    *script,
                    *rest,
                    "--sid",
                    SID,
                    "--resume",
                    "--input",
                    "Continue the unfinished task.",
                ],
            ),
        ]
        for kind, resume, want in cases:
            with self.subTest(kind=kind, resume=resume):
                e = good(kind=kind, resume=list(resume))
                self.assertEqual(roster.recovery(e), want)
                self.assertEqual(e["resume"], resume)

    def test_recovery_reads_the_config_only_for_a_worker_without_a_client(self):
        self.local('runtime = ""\n')
        head = ["/usr/bin/python3", *MODULE, "start", "w1"]
        script = ["/usr/bin/python3", "/x/router.py", "start", "w1"]
        for kind, resume in (
            ("worker", [*head, "--client", "other"]),
            ("worker", head[:5]),
            ("pipeline", script),
            ("role", script),
        ):
            with self.subTest(kind=kind, resume=resume):
                got = roster.recovery(good(kind=kind, resume=resume))
                self.assertEqual(got[: len(resume)], resume)
        with self.assertRaises(roster.RosterError) as cm:
            roster.recovery(good(resume=head))
        self.assertEqual(str(cm.exception), NO_RUNTIME)

    def test_printable(self):
        for text in ("", "plain text", "caf\xe9 \u65e5\u672c", "\xa0", "\u200b", "a b", "~"):
            with self.subTest(text=text):
                self.assertIs(roster.printable(text), True)
        for text in (
            "\n",
            "\r",
            "\t",
            "\x00",
            "\x1b",
            "\x7f",
            "\x80",
            "\x85",
            "\x9f",
            "\u2028",
            "\u2029",
            "a\nb",
        ):
            with self.subTest(text=text):
                self.assertIs(roster.printable(text), False)

    def test_sid(self):
        self.assertTrue(roster.SID.fullmatch(SID))
        for text in (SID.upper(), SID + "\n", SID[:-1], "", "; rm -rf ~", SID.replace("-", "")):
            with self.subTest(text=text):
                self.assertIsNone(roster.SID.fullmatch(text))

    def test_constants(self):
        self.assertEqual(
            (
                roster.VERSION,
                roster.NOTE_MAX,
                roster.ID_MAX,
                roster.LOCK_TIMEOUT,
                roster.ROSTER_MAX,
                roster.EVENTS_MAX,
                roster.SETTLE,
            ),
            (1, 500, 200, 30, 1 << 20, 1 << 20, 1),
        )
        self.assertEqual(roster.KINDS, ("worker", "role", "pipeline"))
        self.assertEqual(
            roster.STATES, ("working", "done", "blocked", "dead", "gone", "waiting", "finished")
        )
        self.assertEqual(
            (roster.ROOT, roster.WORKERS, roster.NAME.pattern),
            ("~/.agent-pm/managers", "~/.agent-pm/workers", "[A-Za-z0-9_-]+"),
        )
        self.assertFalse(hasattr(roster, "LEGACY_CLIENT"))
        for error in (roster.Held, roster.Stale):
            self.assertTrue(issubclass(error, roster.RosterError), error)


class WorkerDirTest(Base):
    def setUp(self):
        super().setUp()
        self.workers = os.path.join(self.agent_pm, "workers")
        self.path = os.path.join(self.workers, SID)

    def refused(self, path, why=NOT_A_DIR):
        with self.assertRaises(roster.RosterError) as cm:
            roster.worker_dir(SID)
        self.assertEqual(str(cm.exception), f"worker directory {path}: {why}")

    def test_made_level_by_level_0700_whatever_the_umask(self):
        for mask in (0o022, 0o077):
            with self.subTest(umask=oct(mask)):
                shutil.rmtree(self.agent_pm)
                self.umask(mask)
                self.assertEqual(roster.worker_dir(SID), self.path)
                for made in (self.agent_pm, self.workers, self.path):
                    self.assertEqual(mode(made), 0o700, made)
                self.assertEqual(os.listdir(self.path), [])
        self.umask(0o277)
        self.assertEqual(roster.worker_dir(SID2), os.path.join(self.workers, SID2))
        self.assertEqual(mode(os.path.join(self.workers, SID2)), 0o700)

    def test_relative_home_gives_an_absolute_path(self):
        base = os.path.dirname(self.agent_pm)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(base)
        os.environ["HOME"] = "."
        self.assertEqual(
            roster.worker_dir(SID), os.path.join(os.getcwd(), ".agent-pm", "workers", SID)
        )

    def test_an_existing_worker_dir_is_kept(self):
        roster.worker_dir(SID)
        with open(os.path.join(self.path, "worker.json"), "w") as f:
            f.write("{}")
        self.assertEqual(roster.worker_dir(SID), self.path)
        self.assertEqual(os.listdir(self.path), ["worker.json"])
        roster.worker_dir(SID2)
        self.assertEqual(sorted(os.listdir(self.workers)), sorted([SID, SID2]))

    def test_a_bad_sid_refused_and_nothing_made(self):
        for sid in (SID.upper(), SID + "\n", SID[:-1], "", "..", f"../{SID}", f"{SID}/x", None, 7):
            with self.subTest(sid=sid), self.assertRaises(roster.RosterError) as cm:
                roster.worker_dir(sid)
            self.assertEqual(str(cm.exception), f"worker sid {sid!r}: want a session id")
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_workers_symlink_refused_and_its_target_untouched(self):
        target = self.outside("target")
        os.symlink(target, self.workers)
        self.refused(self.workers)
        self.assertEqual(os.listdir(target), [])

    def test_sid_symlink_refused_and_its_target_untouched(self):
        target = self.outside("target")
        os.mkdir(self.workers, 0o700)
        os.symlink(target, self.path)
        self.refused(self.path)
        self.assertEqual(os.listdir(target), [])

    def test_a_file_level_refused(self):
        open(self.workers, "w").close()
        self.refused(self.workers)
        os.unlink(self.workers)
        os.mkdir(self.workers, 0o700)
        open(self.path, "w").close()
        self.refused(self.path)

    def test_a_level_of_another_user_refused(self):
        os.mkdir(self.workers, 0o700)
        with unittest.mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.refused(self.workers)
        real = os.lstat

        def lstat(path, *args, **kw):
            st = real(path, *args, **kw)
            return (
                types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1)
                if path == self.path
                else st
            )

        with unittest.mock.patch("os.lstat", lstat):
            self.refused(self.path)

    def test_a_level_open_to_group_or_other_refused_and_not_chmodded(self):
        for level, bits in (
            (self.workers, 0o755),
            (self.workers, 0o701),
            (self.path, 0o750),
            (self.path, 0o707),
        ):
            with self.subTest(level=level, bits=oct(bits)):
                roster.worker_dir(SID)
                os.chmod(level, bits)
                self.refused(level, f"group or other permission bits set (mode {bits:04o})")
                self.assertEqual(mode(level), bits)
                os.chmod(level, 0o700)

    def test_agent_pm_a_file_refused(self):
        os.rmdir(self.agent_pm)
        open(self.agent_pm, "w").close()
        with self.assertRaisesRegex(roster.RosterError, f"^worker directory {self.agent_pm}: "):
            roster.worker_dir(SID)


class DefaultRuntimeTest(Base):
    def test_the_core_configs_runtime_with_the_local_file_on_top(self):
        with open(agent_pm.delegate.engine.config.CONFIG, "rb") as f:
            base = tomllib.load(f)["runtime"]
        self.assertEqual(roster.default_runtime(), base)
        self.local('runtime = "rt9"\n')
        self.assertEqual(roster.default_runtime(), "rt9")

    def test_a_runtime_not_a_non_empty_string_refused(self):
        for value in ('""', "5", "true", "[]", "{}", '["rt9"]'):
            with self.subTest(value=value):
                self.local(f"runtime = {value}\n")
                with self.assertRaises(roster.RosterError) as cm:
                    roster.default_runtime()
                self.assertEqual(str(cm.exception), NO_RUNTIME)

    def test_the_config_paths_given(self):
        config = os.path.join(self.home, "core.toml")
        with open(config, "w") as f:
            f.write('runtime = "rt7"\n')
        self.local('runtime = "rt9"\n')
        self.assertEqual(roster.default_runtime(config, os.devnull), "rt7")
        local = os.path.join(self.agent_pm, "core-new.local.toml")
        self.assertEqual(roster.default_runtime(config, local), "rt9")

    def test_an_unreadable_local_file_refused(self):
        self.local("runtime = \n")
        with self.assertRaisesRegex(roster.RosterError, "^core config: .*line 1"):
            roster.default_runtime()


NOW = object()
ATTACH = "manager directory {} not attached: run crew.py attach first"
KEPT = object()


class LeaseTest(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.dir, "roster.json")
        roster.ensure(self.dir)

    def doc(self, holder=None):
        return {"version": 1, "holder": holder, "cursor": 3, "gen": 1, "entries": {}}

    def held(self, session="other", since=WHEN):
        return {"session": session, "since": since}

    def message(self, session="other", since=WHEN):
        return (
            f"manager directory {self.dir}: held by tmux session {session} since {since}; "
            "ask that manager to run crew.py release, or end that session"
        )

    def refused(self, call, r, message, kind=roster.RosterError):
        before = json.dumps(r)
        with self.assertRaises(kind) as cm:
            call()
        self.assertIs(type(cm.exception), kind)
        self.assertEqual(str(cm.exception), message)
        self.assertEqual(json.dumps(r), before)

    def test_take_and_check_by_holder_own_and_live_sessions(self):
        since = "2026-01-02T03:04:05+00:00"
        other, me = self.held("other", since), self.held("me")
        held, attach = (
            (roster.Held, self.message("other", since)),
            (roster.RosterError, ATTACH.format(self.dir)),
        )
        cases = [
            ("free", None, "me", (), NOW, attach, []),
            ("own the holder: since kept", me, "me", ("me",), KEPT, KEPT, []),
            ("a live other", other, "me", ("other",), held, held, ["other"]),
            ("a dead other: a new since", other, "me", ("me",), NOW, attach, ["other"]),
            ("unknown sessions count as not live", other, "me", None, NOW, attach, ["other"]),
            ("outside tmux, free", None, None, (), KEPT, KEPT, []),
            ("outside tmux, a dead other", other, None, (), KEPT, KEPT, ["other"]),
            ("outside tmux, a live other", other, None, ("other",), held, held, ["other"]),
            ("outside tmux, unknown sessions", other, None, None, KEPT, KEPT, ["other"]),
        ]
        for label, holder, own, live, take, check, asked in cases:
            for call, want in (("take", take), ("check", check)):
                with self.subTest(label, call=call):
                    self.tmux.alive, self.tmux.calls = set(live or ()), []
                    self.tmux.error = (
                        api.DriverError("tmux sessions unknown") if live is None else None
                    )
                    r, start = self.doc(holder), datetime.datetime.now(datetime.UTC)
                    run = (
                        (lambda: roster.take(r, self.dir, own))
                        if call == "take"
                        else (lambda: roster.check(r, self.dir, own))
                    )
                    if isinstance(want, tuple):
                        self.refused(run, r, want[1], want[0])
                    elif want is NOW:
                        run()
                        self.assertEqual(
                            r, self.doc({"session": own, "since": r["holder"]["since"]})
                        )
                        made = datetime.datetime.fromisoformat(r["holder"]["since"])
                        self.assertGreaterEqual(made, start.replace(microsecond=0))
                    else:
                        run()
                        self.assertEqual(r, self.doc(holder if want is KEPT else want))
                    self.assertEqual(self.tmux.calls, [("sessions", re.escape(s)) for s in asked])

    def test_take_under_roster_writes_the_holder_and_a_refusal_writes_nothing(self):
        with roster.locked(self.dir) as r:
            roster.take(r, self.dir, "me")
        with open(self.path, "rb") as f:
            raw = f.read()
        holder = json.loads(raw)["holder"]
        self.assertEqual(holder["session"], "me")
        self.assertIsNotNone(datetime.datetime.fromisoformat(holder["since"]).utcoffset())
        self.tmux.alive = {"me"}
        with self.assertRaises(roster.Held), roster.locked(self.dir) as r:
            roster.take(r, self.dir, "you")
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), raw)

    def test_check_in_a_read_only_block_creates_no_roster_json(self):
        with roster.locked(self.dir, write=False) as r:
            roster.check(r, self.dir, None)
            with self.assertRaises(roster.RosterError):
                roster.check(r, self.dir, "me")
        self.assertFalse(os.path.exists(self.path))

    def test_two_processes_take_concurrently_and_exactly_one_holds(self):
        procs = [
            subprocess.Popen(
                [*PY, "-c", LEASE_CHILD, self.dir, own],
                env=child_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for own in ("a", "b")
        ]
        for p in procs:
            p.stdin.write("\n")
            p.stdin.flush()  # communicate closes it: on 3.11 it flushes stdin first, so a closed one raises
        out = {}
        for own, p in zip("ab", procs, strict=False):
            out[own] = p.communicate(timeout=60)
            self.assertIn(p.returncode, (0, 3), out[own][1])
        winners = [own for own, p in zip("ab", procs, strict=False) if p.returncode == 0]
        self.assertEqual(len(winners), 1)
        loser = "b" if winners == ["a"] else "a"
        with open(self.path) as f:
            holder = json.load(f)["holder"]
        self.assertEqual(holder["session"], winners[0])
        self.assertEqual(out[loser][0].strip(), self.message(winners[0], holder["since"]))


def kills(*sessions):
    return [step for s in sessions for step in (("close", s), ("sessions", re.escape(s)))]


class StopTest(Base):
    def setUp(self):
        super().setUp()
        roster.ensure(self.dir)
        self.entries = {
            "w1": good(),
            "w2": good(sid=SID2),
            "d1": good(kind="role", tui="t1"),
            "d2": good(kind="pipeline", tui="t2"),
            "d3": good(kind="role", tui=None),
        }

    def live(self, *live, stuck=(), unknown=False):
        self.tmux.alive, self.tmux.stuck, self.tmux.calls = set(live), set(stuck), []
        self.tmux.error = api.DriverError("tmux sessions unknown") if unknown else None

    def stop(self, name):
        with roster.locked(self.dir) as r:
            r["entries"] = dict(self.entries)
        with roster.locked(self.dir) as r:
            key = roster.stop(r, name)
        with roster.locked(self.dir, write=False) as r:
            return key, r["entries"]

    def refused(self, name, message):
        with roster.locked(self.dir) as r:
            r["entries"] = dict(self.entries)
        with self.assertRaises(roster.RosterError) as cm, roster.locked(self.dir) as r:
            roster.stop(r, name)
        self.assertEqual(str(cm.exception), message)
        with roster.locked(self.dir, write=False) as r:
            self.assertEqual(r["entries"], self.entries)

    def test_closes_the_sessions_driver_first_then_removes_the_entry(self):
        cases = [
            ("a worker", "w1", ("w1", "w2"), "w1", ("w1",)),
            ("a role by its key", "d1", ("d1", "t1"), "d1", ("d1", "t1")),
            ("a role by its tui", "t1", ("d1", "t1"), "d1", ("d1", "t1")),
            ("a pipeline by its key", "d2", ("d2", "t2"), "d2", ("d2", "t2")),
            ("a pipeline by its tui", "t2", ("d2", "t2"), "d2", ("d2", "t2")),
            ("a worker gone already", "w1", (), "w1", ("w1",)),
            ("a role's driver gone already", "d1", ("t1",), "d1", ("d1", "t1")),
            ("a role without a tui, gone already", "d3", (), "d3", ("d3",)),
            ("unknown sessions count as gone", "w1", None, "w1", ("w1",)),
        ]
        for label, name, live, key, closed in cases:
            with self.subTest(label):
                self.live(*(live or ()), unknown=live is None)
                self.assertEqual(
                    self.stop(name),
                    (key, {n: e for n, e in self.entries.items() if n != key}),
                )
                self.assertEqual(self.tmux.calls, kills(*closed))

    def test_an_entry_key_beats_another_entrys_tui(self):
        self.entries["t1"] = good()
        self.live("t1", "d1")
        self.assertEqual(self.stop("t1")[0], "t1")
        self.assertEqual(self.tmux.calls, kills("t1"))

    def test_not_in_roster_or_a_session_still_live_keeps_every_entry(self):
        cases = [
            ("not in roster: nothing closed", "w9", ("w9",), (), "w9 not in roster", ()),
            (
                "a close failing, its session still live",
                "d1",
                ("d1", "t1"),
                ("t1",),
                "d1: session t1 still live after tmux kill-session",
                ("d1", "t1"),
            ),
        ]
        for label, name, live, stuck, message, closed in cases:
            with self.subTest(label):
                self.live(*live, stuck=stuck)
                self.refused(name, message)
                self.assertEqual(self.tmux.calls, kills(*closed))


WIDTH = 64
SPAN = roster.EVENTS_MAX // WIDTH
WRITER = b"12:00:00 w9 done\n"


def numbered(count, *, start=1, first=WIDTH):
    return b"".join(
        f"line {i}".ljust((first if i == start else WIDTH) - 1, ".").encode() + b"\n"
        for i in range(start, start + count)
    )


class CursorTest(Base):
    def setUp(self):
        super().setUp()
        self.events = os.path.join(self.dir, "events")
        self.old = self.events + ".1"
        self.path = os.path.join(self.dir, "roster.json")
        roster.ensure(self.dir)

    def doc(self, cursor=0, gen=0):
        return {"version": 1, "holder": None, "cursor": cursor, "gen": gen, "entries": {}}

    def write(self, data, path=None):
        with open(path or self.events, "wb") as f:
            f.write(data)
        os.chmod(path or self.events, 0o600)
        return data

    def read(self, path=None):
        with open(path or self.events, "rb") as f:
            return f.read()

    def due_file(self, tail=b""):
        self.write(numbered(SPAN + 3) + tail)
        return SPAN

    def rotate(self, r, during=lambda: None):
        slept, err = [], io.StringIO()

        def sleep(seconds):
            slept.append(seconds)
            during()

        with contextlib.redirect_stderr(err):
            result = roster.rotate(r, self.dir, sleep=sleep)
        return result, slept, err.getvalue()

    def lstat_as(self, path, change):
        real = os.lstat

        def lstat(p, *args, **kw):
            st = real(p, *args, **kw)
            return change(p, st) if p == path else st

        return unittest.mock.patch("os.lstat", lstat)

    def after_rename(self, step):
        real = os.replace

        def replace(src, dst, *args, **kw):
            real(src, dst, *args, **kw)
            if dst == self.old:
                step()

        return unittest.mock.patch("os.replace", replace)

    def test_missing_or_empty_has_no_lines(self):
        for label, make in (("missing", lambda: None), ("empty", lambda: self.write(b""))):
            with self.subTest(label):
                make()
                self.assertEqual(roster.lines(self.events), 0)
                self.assertEqual([roster.offset(self.events, n) for n in (0, 3)], [0, 0])
                self.assertEqual([roster.following(self.events, n) for n in (0, 3)], [b"", b""])

    def test_a_trailing_fragment_is_not_a_line_and_follows_as_is(self):
        self.write(b"a\nbb\nccc")
        self.assertEqual(roster.lines(self.events), 2)
        self.assertEqual([roster.offset(self.events, n) for n in range(5)], [0, 2, 5, 8, 8])
        self.assertEqual(
            [roster.following(self.events, n) for n in range(4)],
            [b"a\nbb\nccc", b"bb\nccc", b"ccc", b""],
        )

    def test_lines_offset_and_following_across_blocks(self):
        data = self.write(numbered(3 * SPAN // 2))
        self.assertEqual(roster.lines(self.events), 3 * SPAN // 2)
        self.assertEqual(roster.offset(self.events, 12345), 12345 * WIDTH)
        self.assertEqual(roster.following(self.events, 12345), data[12345 * WIDTH :])
        self.assertEqual(
            roster.following(self.events, 3 * SPAN // 2 - 2), numbered(2, start=3 * SPAN // 2 - 1)
        )

    def test_symlink_and_fifo_refused_without_blocking(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        with open(target, "wb") as f:
            f.write(b"a\nb\n")
        reads = (roster.lines, lambda p: roster.offset(p, 1), lambda p: roster.following(p, 1))
        os.symlink(target, self.events)
        for kind in ("symlink", "fifo"):
            if kind == "fifo":
                os.unlink(self.events)
                os.mkfifo(self.events)
            for read in reads:
                with self.subTest(kind), within(), self.assertRaises(roster.RosterError) as cm:
                    read(self.events)
                self.assertEqual(str(cm.exception), NOT_REGULAR.format(self.events))
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"a\nb\n")

    def test_due_boundaries(self):
        half = roster.EVENTS_MAX // 2
        cases = [
            ("exactly EVENTS_MAX, all handled", numbered(SPAN), SPAN, roster.EVENTS_MAX, False),
            (
                "over, handled one byte short",
                numbered(SPAN + 1, first=WIDTH - 1),
                SPAN // 2,
                half - 1,
                False,
            ),
            ("over, handled exactly half", numbered(SPAN + 1), SPAN // 2, half, True),
            ("over, cursor 0", numbered(2 * SPAN), 0, 0, False),
        ]
        for label, data, cursor, handled, want in cases:
            with self.subTest(label):
                self.write(data)
                self.assertEqual(roster.offset(self.events, cursor), handled)
                r = self.doc(cursor)
                self.assertIs(roster.due(r, self.dir), want)
                self.assertEqual(r, self.doc(cursor))
        os.unlink(self.events)
        self.assertIs(roster.due(self.doc(5), self.dir), False)

    def test_advance_checks_the_gen_first(self):
        self.write(numbered(5))
        r = self.doc(2, 1)
        for after in (3, 9, None):
            with self.subTest(after=after), self.assertRaises(roster.Stale) as cm:
                roster.advance(r, self.dir, after, 0)
            self.assertEqual(
                str(cm.exception), "stale line numbers (gen 0, now 1): run crew.py attach"
            )
        self.assertEqual(r, self.doc(2, 1))

    def test_advance_past_the_end_is_stale(self):
        self.write(numbered(5) + b"line 6")
        r = self.doc(2, 1)
        with self.assertRaises(roster.Stale) as cm:
            roster.advance(r, self.dir, 6, 1)
        self.assertEqual(
            str(cm.exception),
            f"line 6 is past the end of {self.events} (5 lines): run crew.py attach",
        )
        self.assertEqual(r, self.doc(2, 1))
        os.unlink(self.events)
        with self.assertRaisesRegex(roster.Stale, r"^line 1 is past the end of .* \(0 lines\)"):
            roster.advance(r, self.dir, 1, 1)

    def test_advance_moves_the_cursor_forward_only_and_returns_the_line(self):
        self.write(numbered(5) + b"line 6")
        cases = [
            (0, 3, 3, 3),
            (3, 1, 3, 1),
            (3, 4, 4, 4),
            (3, 5, 5, 5),
            (0, None, 5, 5),
            (5, None, 5, 5),
            (0, 0, 0, 0),
        ]
        for cursor, after, moved, resolved in cases:
            with self.subTest(cursor=cursor, after=after):
                r = self.doc(cursor, 2)
                self.assertEqual(roster.advance(r, self.dir, after, 2), resolved)
                self.assertEqual(r, self.doc(moved, 2))

    def test_advance_on_a_missing_events_file(self):
        r = self.doc()
        self.assertEqual([roster.advance(r, self.dir, after, 0) for after in (0, None)], [0, 0])
        self.assertEqual(r, self.doc())

    def test_rotate_not_due_touches_nothing(self):
        for label, data, cursor in (
            ("exactly EVENTS_MAX", numbered(SPAN), SPAN),
            ("cursor 0", numbered(2 * SPAN), 0),
            ("missing", None, 0),
        ):
            with self.subTest(label):
                if data is None:
                    os.unlink(self.events)
                else:
                    self.write(data)
                r = self.doc(cursor, 2)
                self.assertEqual(self.rotate(r), (False, [], ""))
                self.assertEqual(r, self.doc(cursor, 2))
                self.assertEqual(os.listdir(self.dir), [] if data is None else ["events"])
                if data is not None:
                    self.assertEqual(self.read(), data)

    def test_rotate_keeps_the_lines_after_the_cursor_in_a_fresh_0600_events(self):
        self.addCleanup(os.umask, os.umask(0o022))
        cases = [
            ("plain", b"", False, b""),
            ("a trailing fragment gets its newline", b"12:00:01 w9 do", False, b"12:00:01 w9 do\n"),
            ("an old events.1 replaced", b"", True, b""),
        ]
        for label, tail, old, ended in cases:
            with self.subTest(label):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.old)
                cursor = self.due_file(tail)
                if old:
                    self.write(b"old\n", self.old)
                original, ino = self.read(), os.lstat(self.events).st_ino
                r = self.doc(cursor, 2)
                self.assertEqual(self.rotate(r), (True, [roster.SETTLE], ""))
                self.assertEqual(r, self.doc(0, 3))
                self.assertEqual((os.lstat(self.old).st_ino, self.read(self.old)), (ino, original))
                self.assertEqual(self.read(), numbered(3, start=SPAN + 1) + ended)
                self.assertTrue(stat.S_ISREG(os.lstat(self.events).st_mode))
                self.assertEqual(mode(self.events), 0o600)
                self.assertNotEqual(os.lstat(self.events).st_ino, ino)
                self.assertEqual(sorted(os.listdir(self.dir)), ["events", "events.1"])

    def test_rotate_puts_a_line_appended_during_the_wait_first(self):
        cursor = self.due_file()

        def append():
            with open(self.events, "ab") as f:
                f.write(WRITER)

        r = self.doc(cursor)
        self.assertEqual(self.rotate(r, append)[:2], (True, [roster.SETTLE]))
        self.assertEqual(self.read(), WRITER + numbered(3, start=SPAN + 1))

    def test_rotate_makes_a_writers_0644_events_0600(self):
        self.addCleanup(os.umask, os.umask(0o022))
        cursor = self.due_file()
        made = []

        def write():
            with open(self.events, "ab") as f:
                f.write(WRITER)
            made.append(mode(self.events))

        with self.after_rename(write):
            self.assertIs(self.rotate(self.doc(cursor))[0], True)
        self.assertEqual(made, [0o644])
        self.assertEqual(mode(self.events), 0o600)
        self.assertEqual(self.read(), WRITER + numbered(3, start=SPAN + 1))

    def test_rotate_refills_no_symlink_or_fifo_made_after_the_rename(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        cases = [
            ("a symlink", lambda: os.symlink(target, self.events), NOT_REGULAR.format(self.events)),
            ("a fifo", lambda: os.mkfifo(self.events), None),
        ]
        for label, make, why in cases:
            with self.subTest(label):
                for p in (self.events, self.old):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(p)
                with open(target, "wb"):
                    pass
                cursor = self.due_file()
                r = self.doc(cursor, 2)
                with self.after_rename(make), within():
                    result, slept, err = self.rotate(r)
                self.assertEqual((result, slept, r), (True, [], self.doc(0, 3)))
                tail = f": lines after {cursor} of {self.old} not copied\n"
                if why is None:
                    self.assertRegex(
                        err, f"^manager: rotate: {re.escape(self.events)}: .+{re.escape(tail)}$"
                    )
                else:
                    self.assertEqual(err, f"manager: rotate: {why}{tail}")
                self.assertEqual(os.path.getsize(target), 0)

    def test_rotate_failing_after_the_rename_still_resets_the_cursor_and_bumps_the_gen(self):
        cursor = self.due_file()
        original = self.read()
        with roster.locked(self.dir) as r:
            r.update(cursor=cursor, gen=2)
        real = os.open

        def open_(path, flags, *args, **kw):
            if path == self.events and flags & os.O_APPEND and flags & os.O_NONBLOCK:
                raise OSError(errno.EIO, "Input/output error")
            return real(path, flags, *args, **kw)

        err = io.StringIO()
        with (
            unittest.mock.patch("os.open", open_),
            contextlib.redirect_stderr(err),
            roster.locked(self.dir) as r,
        ):
            self.assertIs(roster.rotate(r, self.dir, sleep=lambda seconds: None), True)
        self.assertEqual(
            err.getvalue(),
            f"manager: rotate: {self.events}: Input/output error: lines after {cursor} of "
            f"{self.old} not copied\n",
        )
        with roster.locked(self.dir, write=False) as r:
            self.assertEqual((r["cursor"], r["gen"]), (0, 3))
        self.assertEqual((self.read(self.old), self.read()), (original, b""))
        self.assertEqual(mode(self.events), 0o600)

    def test_rotate_failing_to_rename_changes_nothing(self):
        cursor = self.due_file()
        original = self.read()
        r = self.doc(cursor, 2)
        with (
            unittest.mock.patch("os.replace", side_effect=OSError(errno.EIO, "Input/output error")),
            self.assertRaises(roster.RosterError) as cm,
        ):
            self.rotate(r)
        self.assertEqual(str(cm.exception), f"{self.events}: Input/output error")
        self.assertEqual(r, self.doc(cursor, 2))
        self.assertEqual((os.listdir(self.dir), self.read()), (["events"], original))

    def test_rotate_refuses_a_symlink_fifo_or_foreign_events_or_events_1(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")

        def foreign(path, st):
            return types.SimpleNamespace(
                st_mode=st.st_mode, st_uid=st.st_uid + 1, st_size=st.st_size
            )

        cases = [
            ("events.1 a symlink", self.old, lambda: os.symlink(target, self.old), None),
            ("events.1 a fifo", self.old, lambda: os.mkfifo(self.old), None),
            ("events.1 foreign", self.old, lambda: self.write(b"old\n", self.old), foreign),
            ("events foreign", self.events, lambda: None, foreign),
            (
                "events a symlink, lstat seeing its target",
                self.events,
                lambda: (os.rename(self.events, target), os.symlink(target, self.events)),
                lambda path, st: os.stat(path),
            ),
        ]
        for label, path, make, change in cases:
            with self.subTest(label):
                for p in (self.events, self.old, target):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(p)
                cursor = self.due_file()
                make()
                inodes = {
                    name: os.lstat(os.path.join(self.dir, name)).st_ino
                    for name in os.listdir(self.dir)
                }
                r = self.doc(cursor, 2)
                with (
                    self.lstat_as(path, change) if change else contextlib.nullcontext(),
                    within(),
                    self.assertRaises(roster.RosterError) as cm,
                ):
                    self.rotate(r)
                self.assertEqual(str(cm.exception), NOT_REGULAR.format(path))
                self.assertEqual(r, self.doc(cursor, 2))
                self.assertEqual(
                    {
                        name: os.lstat(os.path.join(self.dir, name)).st_ino
                        for name in os.listdir(self.dir)
                    },
                    inodes,
                )

    def test_a_refusal_inside_roster_leaves_roster_json_unchanged(self):
        cursor = self.due_file()
        with roster.locked(self.dir) as r:
            r.update(cursor=cursor, gen=2)
        with open(self.path, "rb") as f:
            raw = f.read()
        os.symlink(os.path.join(os.path.dirname(self.agent_pm), "target"), self.old)
        with self.assertRaises(roster.RosterError), roster.locked(self.dir) as r:
            roster.rotate(r, self.dir, sleep=lambda seconds: None)
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), raw)
        self.assertTrue(os.path.islink(self.old))


if __name__ == "__main__":
    unittest.main()
