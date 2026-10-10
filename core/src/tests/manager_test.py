import contextlib
import datetime
import fcntl
import io
import json
import os
import signal
import stat
import subprocess
import sys
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hermetic  # noqa: E402
import manager  # noqa: E402

INSIDE = {"TMUX": "/tmp/tmux-501/default,1,0", "TMUX_PANE": "%3"}


def tmux(out="own\n"):
    """A proc answering every call with out; its calls are recorded."""
    def proc(argv, **kw):
        proc.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, out, "")

    proc.calls = []
    return proc


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class ManagerTest(unittest.TestCase):
    def setUp(self):
        self.agent_pm = hermetic.home(self)
        self.managers = os.path.join(self.agent_pm, "managers")
        self.dir = os.path.join(self.managers, "m1")
        self.enterContext(unittest.mock.patch.dict(os.environ))
        for key in INSIDE:
            os.environ.pop(key, None)

    def umask(self, mask):
        self.addCleanup(os.umask, os.umask(mask))

    def outside(self, name):
        """A directory beside ~/.agent-pm, for a link to point at."""
        path = os.path.join(os.path.dirname(self.agent_pm), name)
        os.mkdir(path)
        return path

    def test_manager_beats_tmux(self):
        proc = tmux()
        os.environ.update(INSIDE)
        self.assertEqual(manager.directory("m1", proc=proc), self.dir)
        self.assertEqual(proc.calls, [])

    def test_own_session(self):
        proc = tmux("own\n")
        os.environ.update(INSIDE)
        self.assertEqual(manager.directory(proc=proc), os.path.join(self.managers, "own"))
        self.assertEqual(len(proc.calls), 1)

    def test_outside_tmux_is_none(self):
        proc = tmux()
        self.assertIsNone(manager.directory(proc=proc))
        self.assertEqual(proc.calls, [])

    def test_resolving_touches_nothing(self):
        os.environ.update(INSIDE)
        manager.directory("m1")
        manager.directory(proc=tmux())
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_relative_home_gives_an_absolute_path(self):
        os.environ["HOME"] = "rel/home"
        want = os.path.join(os.path.abspath("rel/home"), ".agent-pm", "managers", "m1")
        self.assertEqual(manager.directory("m1"), want)

    def test_bad_manager_names(self):
        for name in ("a/b", "..", "", "a b", "a;", "a\n"):
            with self.subTest(name=name), self.assertRaises(manager.ManagerError) as cm:
                manager.directory(name)
            self.assertEqual(str(cm.exception), f"manager {name!r}: want [A-Za-z0-9_-]+")

    def test_bad_own_session_name_names_manager_option(self):
        os.environ.update(INSIDE)
        with self.assertRaises(manager.ManagerError) as cm:
            manager.directory(proc=tmux("a;\n"))
        self.assertEqual(str(cm.exception), "own tmux session 'a;': want [A-Za-z0-9_-]+; give --manager <name>")

    def test_bad_tmux_pane_names_manager_option(self):
        os.environ.update(INSIDE, TMUX_PANE="%3;")
        proc = tmux()
        with self.assertRaisesRegex(manager.ManagerError, r"TMUX_PANE.*; give --manager <name>$"):
            manager.directory(proc=proc)
        self.assertEqual(proc.calls, [])

    def test_home_is_the_directory_whose_events_file_is_given(self):
        manager.ensure(self.dir)
        proc = tmux()
        self.assertEqual(manager.home("m1", os.path.join(self.dir, "events"), proc=proc), self.dir)
        self.assertEqual(proc.calls, [])
        self.assertEqual(os.listdir(self.dir), [])

    def test_home_is_none_without_the_directory_or_its_events_file(self):
        manager.ensure(self.dir)
        events = os.path.join(self.dir, "events")
        cases = {"events another file": ("m1", os.path.join(self.outside("x"), "events")),
                 "events a sibling file": ("m1", os.path.join(self.dir, "roster.json")),
                 "events None": ("m1", None),
                 "directory missing": ("m2", os.path.join(self.managers, "m2", "events")),
                 "outside tmux, no name": (None, events)}
        for why, (name, given) in cases.items():
            with self.subTest(why):
                self.assertIsNone(manager.home(name, given, proc=tmux()))
        self.assertEqual(os.listdir(self.managers), ["m1"])
        self.assertEqual(os.listdir(self.dir), [])

    def test_home_of_the_own_tmux_session(self):
        own = os.path.join(self.managers, "own")
        manager.ensure(own)
        os.environ.update(INSIDE)
        self.assertEqual(manager.home(None, os.path.join(own, "events"), proc=tmux("own\n")), own)

    def test_home_compares_real_paths(self):
        manager.ensure(self.dir)
        base = os.path.dirname(self.agent_pm)
        os.symlink(self.dir, os.path.join(base, "link"))
        self.assertEqual(manager.home("m1", os.path.join(base, "link", "events")), self.dir)
        os.symlink(base, os.path.join(base, "alias"))
        os.environ["HOME"] = os.path.join(base, "alias")
        self.assertEqual(manager.home("m1", os.path.join(self.dir, "events")),
                         os.path.join(base, "alias", ".agent-pm", "managers", "m1"))

    def test_home_bad_name_or_own_session_is_a_manager_error(self):
        with self.assertRaisesRegex(manager.ManagerError, r"^manager 'a/b': want \[A-Za-z0-9_-\]\+$"):
            manager.home("a/b", None)
        os.environ.update(INSIDE)
        with self.assertRaisesRegex(manager.ManagerError, "give --manager <name>$"):
            manager.home(None, None, proc=tmux("a;\n"))

    def test_events_path_without_create_touches_nothing(self):
        self.assertEqual(manager.events(self.dir, create=False), os.path.join(self.dir, "events"))
        self.assertEqual(os.listdir(self.agent_pm), [])

    def check_created(self, mask):
        os.rmdir(self.agent_pm)
        self.umask(mask)
        path = manager.events(self.dir)
        self.assertEqual(path, os.path.join(self.dir, "events"))
        for made in (self.agent_pm, self.managers, self.dir):
            self.assertEqual(mode(made), 0o700, made)
        self.assertTrue(stat.S_ISREG(os.lstat(path).st_mode))
        self.assertEqual(mode(path), 0o600)

    def test_created_under_umask_022(self):
        self.check_created(0o022)

    def test_created_under_umask_077(self):
        self.check_created(0o077)

    def test_directories_are_0700_even_when_the_umask_strips_owner_bits(self):
        self.umask(0o277)
        manager.ensure(self.dir)
        self.assertEqual([mode(self.managers), mode(self.dir)], [0o700, 0o700])

    def test_existing_managers_directory_keeps_its_mode(self):
        os.mkdir(self.managers)
        os.chmod(self.managers, 0o755)
        manager.ensure(self.dir)
        self.assertEqual([mode(self.managers), mode(self.dir)], [0o755, 0o700])

    def test_existing_directory_is_kept(self):
        manager.ensure(self.dir)
        with open(manager.events(self.dir), "w") as f:
            f.write("12:00:00 w1 done\n")
        manager.ensure(self.dir)
        self.assertEqual(mode(self.dir), 0o700)
        with open(os.path.join(self.dir, "events")) as f:
            self.assertEqual(f.read(), "12:00:00 w1 done\n")

    def test_directory_open_to_group_or_other_refused_and_not_chmodded(self):
        manager.ensure(self.dir)
        for bits in (0o750, 0o705, 0o770, 0o777, 0o701):
            os.chmod(self.dir, bits)
            for call in (manager.ensure, manager.events):
                with self.subTest(bits=oct(bits), call=call.__name__), self.assertRaisesRegex(
                        manager.ManagerError, f"^manager directory {self.dir}: .*group or other"):
                    call(self.dir)
            self.assertEqual(mode(self.dir), bits)
            self.assertEqual(os.listdir(self.dir), [])

    def refused(self, path):
        with self.assertRaises(manager.ManagerError) as cm:
            manager.events(self.dir)
        self.assertEqual(str(cm.exception), f"manager directory {path}: not a directory owned by you")

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
        with self.assertRaisesRegex(manager.ManagerError, f"^manager directory {self.agent_pm}: "):
            manager.ensure(self.dir)

    def test_events_symlink_refused(self):
        manager.ensure(self.dir)
        target = os.path.join(self.outside("target"), "file")
        open(target, "w").close()
        os.symlink(target, os.path.join(self.dir, "events"))
        with self.assertRaisesRegex(manager.ManagerError, "events file .*events"):
            manager.events(self.dir)
        self.assertEqual(os.path.getsize(target), 0)


SID = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
SID2 = "1b2c3d4e-5f60-7890-abcd-ef0123456789"
WHEN = "2026-01-01T00:00:00+00:00"
SRC = os.path.dirname(os.path.abspath(manager.__file__))
CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
import manager
for i in range(15):
    manager.put(sys.argv[2], sys.argv[3] + str(i), manager.entry("worker", sid=None, cwd="/w", resume=["/p", "/x/workers.py"]))
"""

LEASE_CHILD = """
import subprocess, sys
sys.path.insert(0, sys.argv[1])
import manager
d, own = sys.argv[2], sys.argv[3]
def proc(argv, **kw):
    return subprocess.CompletedProcess(argv, 0, "", "")
sys.stdin.readline()
try:
    with manager.roster(d) as r:
        manager.take(r, d, own, proc=proc)
except manager.Held as e:
    print(e)
    sys.exit(3)
"""


def good(**over):
    """A valid entry value."""
    value = dict(kind="worker", sid=SID, cwd="/w", resume=["/usr/bin/python3", "/x/workers.py"], note=None, opener=None,
                 pane=None, split=None, split_from=None, tui=None, state="working", started=WHEN)
    return {**value, **over}


KIND = "kind: {!r}: want worker, role or pipeline"
VALID = {
    "kind": ["worker", "role", "pipeline"],
    "sid": [SID, None],
    "cwd": ["/w", "/w with space/é日", "/w ​"],
    "resume": [["/usr/bin/python3", "/x/workers.py", "; rm -rf ~"], ["/p", "/x/drive.py"], ["/p", "/x/router.py", "a", "b"]],
    "note": [None, "", "x" * 500, "né ​"],
    "opener": [None, "mgr", "w0t0p0:5E1B-C0FFEE"],
    "pane": [None, "%3", "5E1B-C0FFEE"],
    "split": [None, "right", "below"],
    "split_from": [None, "mgr"],
    "tui": [None, "mgr"],
    "state": ["working", "done", "blocked", "dead", "gone", "finished"],
    "started": [WHEN, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00-05:00"],
}
RESUME_LIST = "a list of 2 or more printable strings"
RESUME_1 = "an absolute path named workers.py, drive.py or router.py"
NOTE = "null or a printable string of at most 500 characters"
CWD = "an absolute printable path"
INVALID = [
    ("kind", "boss", KIND), ("kind", None, KIND), ("kind", ["worker"], KIND),
    ("sid", "; rm -rf ~", "sid: {!r}: want a session id or null"), ("sid", SID.upper(), "sid: {!r}: want a session id or null"),
    ("sid", SID + "\n", "sid: {!r}: want a session id or null"), ("sid", 7, "sid: {!r}: want a session id or null"),
    ("cwd", "rel/w", f"cwd: {{!r}}: want {CWD}"), ("cwd", "/w\nx", f"cwd: {{!r}}: want {CWD}"),
    ("cwd", "/w\x85", f"cwd: {{!r}}: want {CWD}"), ("cwd", "/w x", f"cwd: {{!r}}: want {CWD}"),
    ("cwd", "/w ", f"cwd: {{!r}}: want {CWD}"), ("cwd", "/w\x00", f"cwd: {{!r}}: want {CWD}"),
    ("cwd", "/w\x7f", f"cwd: {{!r}}: want {CWD}"), ("cwd", None, f"cwd: {{!r}}: want {CWD}"),
    ("resume", ["/usr/bin/python3"], f"resume: {{!r}}: want {RESUME_LIST}"), ("resume", "x", f"resume: {{!r}}: want {RESUME_LIST}"),
    ("resume", ["/p", "/x/workers.py", "a\nb"], f"resume: {{!r}}: want {RESUME_LIST}"),
    ("resume", ["/p", "/x/workers.py", "a "], f"resume: {{!r}}: want {RESUME_LIST}"),
    ("resume", ["/p", "/x/workers.py", 3], f"resume: {{!r}}: want {RESUME_LIST}"),
    ("resume", ["python3", "/x/workers.py"], "resume[0]: 'python3': want an absolute path"),
    ("resume", ["/p", "workers.py"], f"resume[1]: 'workers.py': want {RESUME_1}"),
    ("resume", ["/p", "/x/other.py"], f"resume[1]: '/x/other.py': want {RESUME_1}"),
    ("resume", ["/p", "/x/workers.py/"], f"resume[1]: '/x/workers.py/': want {RESUME_1}"),
    ("note", "a\nb", f"note: {{!r}}: want {NOTE}"), ("note", "x" * 501, f"note: {{!r}}: want {NOTE}"),
    ("note", "a\x9fb", f"note: {{!r}}: want {NOTE}"), ("note", "a b", f"note: {{!r}}: want {NOTE}"),
    ("note", 5, f"note: {{!r}}: want {NOTE}"),
    ("opener", "a b", "opener: {!r}: want a tmux session name or an iTerm2 pane id, or null"),
    ("opener", "w0t0p0:", "opener: {!r}: want a tmux session name or an iTerm2 pane id, or null"),
    ("opener", 5, "opener: {!r}: want a tmux session name or an iTerm2 pane id, or null"),
    ("pane", "a b", "pane: {!r}: want a tmux pane id or an iTerm2 session id, or null"),
    ("pane", "%x", "pane: {!r}: want a tmux pane id or an iTerm2 session id, or null"),
    ("pane", "%3 ", "pane: {!r}: want a tmux pane id or an iTerm2 session id, or null"),
    ("pane", "", "pane: {!r}: want a tmux pane id or an iTerm2 session id, or null"),
    ("split", "left", "split: {!r}: want right, below or null"), ("split", "", "split: {!r}: want right, below or null"),
    ("split_from", "a b", "split_from: {!r}: want a session name or null"),
    ("split_from", "", "split_from: {!r}: want a session name or null"),
    ("tui", "a;", "tui: {!r}: want a session name or null"),
    ("state", "idle", "state: {!r}: want working, done, blocked, dead, gone or finished"),
    ("state", None, "state: {!r}: want working, done, blocked, dead, gone or finished"),
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
    ("holder with an extra key", {"holder": {"session": "s", "since": WHEN, "x": 1}}, "holder: keys: unexpected ['x']"),
    ("holder session", {"holder": {"session": "a b", "since": WHEN}}, "holder.session: 'a b': want a session name"),
    ("holder since naive", {"holder": {"session": "s", "since": "2026-01-01T00:00:00"}},
     "holder.since: '2026-01-01T00:00:00': want an ISO 8601 time with a UTC offset"),
    ("cursor negative", {"cursor": -1}, "cursor: -1: want a non-negative integer"),
    ("cursor true", {"cursor": True}, "cursor: True: want a non-negative integer"),
    ("cursor float", {"cursor": 1.0}, "cursor: 1.0: want a non-negative integer"),
    ("gen a string", {"gen": "0"}, "gen: '0': want a non-negative integer"),
    ("gen null", {"gen": None}, "gen: None: want a non-negative integer"),
    ("entries a list", {"entries": []}, "entries: want an object"),
]


class RosterTest(unittest.TestCase):
    def setUp(self):
        self.agent_pm = hermetic.home(self)
        self.managers = os.path.join(self.agent_pm, "managers")
        self.dir = os.path.join(self.managers, "m1")
        self.path = os.path.join(self.dir, "roster.json")
        self.lock = os.path.join(self.dir, "roster.lock")
        manager.ensure(self.dir)

    def doc(self, **top):
        return {"version": 1, "holder": None, "cursor": 0, "gen": 0, "entries": {}, **top}

    def store(self, data):
        """Writes roster.json as given (bytes, str or JSON-able); returns its bytes."""
        raw = data if isinstance(data, bytes) else data.encode() if isinstance(data, str) else json.dumps(data).encode()
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
        """The roster as read (write=False unless given) and what it printed to stderr."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), manager.roster(self.dir, **{"write": False, **kw}) as r:
            pass
        return r, err.getvalue()

    def refused(self, message, **kw):
        with self.assertRaises(manager.ManagerError) as cm, manager.roster(self.dir, **kw):
            pass
        self.assertEqual(str(cm.exception), message)

    def hold(self):
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)

    def foreign(self, path):
        """os.fstat answering for `path`'s file as another user's."""
        ino, real = os.stat(path).st_ino, os.fstat

        def fstat(fd):
            st = real(fd)
            if st.st_ino != ino:
                return st
            return types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1, st_size=st.st_size)

        return unittest.mock.patch("os.fstat", fstat)

    @contextlib.contextmanager
    def within(self, seconds=10):
        def fire(*_):
            raise AssertionError("blocked")

        old = signal.signal(signal.SIGALRM, fire)
        signal.alarm(seconds)
        try:
            yield
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    def test_fresh_roster_is_written_0600_with_the_lock(self):
        self.addCleanup(os.umask, os.umask(0))
        with manager.roster(self.dir) as r:
            self.assertEqual(r, self.doc())
        with open(self.path) as f:
            self.assertEqual(f.read(), json.dumps(self.doc(), indent=2, sort_keys=True) + "\n")
        self.assertEqual([mode(self.path), mode(self.lock), mode(self.dir)], [0o600, 0o600, 0o700])
        self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])
        self.assertEqual(self.load()[0], self.doc())

    def test_changes_are_written_and_read_back(self):
        holder = {"session": "s1", "since": WHEN}
        with manager.roster(self.dir) as r:
            r.update(holder=holder, cursor=4, gen=2)
            r["entries"]["w1"] = good(note="n")
        r, err = self.load()
        self.assertEqual(r, self.doc(holder=holder, cursor=4, gen=2, entries={"w1": good(note="n")}))
        self.assertEqual(err, "")
        self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])

    def test_unchanged_roster_is_not_rewritten(self):
        raw = self.store(json.dumps(self.doc(entries={"w1": good()}), separators=(",", ":")))
        with manager.roster(self.dir):
            pass
        self.assertEqual(self.bytes(), raw)

    def test_write_false_writes_nothing_even_when_the_file_is_missing(self):
        with manager.roster(self.dir, write=False) as r:
            r["cursor"] = 9
        self.assertFalse(os.path.exists(self.path))
        raw = self.store(self.doc())
        with manager.roster(self.dir, write=False) as r:
            r["cursor"] = 9
        self.assertEqual(self.bytes(), raw)

    def test_exception_in_the_block_writes_nothing(self):
        with self.assertRaises(RuntimeError), manager.roster(self.dir) as r:
            r["cursor"] = 9
            raise RuntimeError
        self.assertFalse(os.path.exists(self.path))
        raw = self.store(self.doc())
        with self.assertRaises(RuntimeError), manager.roster(self.dir) as r:
            r["cursor"] = 9
            raise RuntimeError
        self.assertEqual(self.bytes(), raw)

    def test_lock_is_released_after_the_block(self):
        with self.assertRaises(RuntimeError), manager.roster(self.dir):
            raise RuntimeError
        fd = os.open(self.lock, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_write_back_validates_the_whole_dict(self):
        raw = self.store(self.doc(entries={"w1": good()}))
        bad = [
            (lambda r: r.update(cursor=-1), "cursor: -1: want a non-negative integer"),
            (lambda r: r.update(x=1), "keys: unexpected ['x']"),
            (lambda r: r["entries"].update(w2=good(kind="boss")), "entry 'w2': " + KIND.format("boss")),
            (lambda r: r["entries"].update({"a b": good()}), "entry 'a b': name: want [A-Za-z0-9_-]+"),
            (lambda r: r["entries"].update(w2=good(resume=("/p", "/x/workers.py"))),
             f"entry 'w2': resume: ('/p', '/x/workers.py'): want {RESUME_LIST}"),
            (lambda r: r["entries"].update(w2={}), "entry 'w2': keys: missing " + str(sorted(good()))),
        ]
        for change, why in bad:
            with self.subTest(why=why), self.assertRaises(manager.ManagerError) as cm:
                with manager.roster(self.dir) as r:
                    change(r)
            self.assertEqual(str(cm.exception), f"{self.path}: {why}")
            self.assertEqual(self.bytes(), raw)
            self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])

    def test_failed_write_removes_its_temp_file_and_keeps_the_old_file(self):
        raw = self.store(self.doc())
        with unittest.mock.patch("os.replace", side_effect=OSError(5, "Input/output error")):
            with self.assertRaises(manager.ManagerError) as cm, manager.roster(self.dir) as r:
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

    def test_entry_keys_must_be_exactly_the_twelve(self):
        extra, missing = {**good(), "x": 1}, {k: v for k, v in good().items() if k != "note"}
        both = {**missing, "y": 1}
        for value, why in [(extra, "keys: unexpected ['x']"), (missing, "keys: missing ['note']"),
                           (both, "keys: missing ['note'], unexpected ['y']"), ([], "value: []: want an object"),
                           ("w", "value: 'w': want an object")]:
            with self.subTest(why=why):
                self.store(self.doc(entries={"bad": value}))
                r, err = self.load()
                self.assertEqual((r["entries"], err), ({}, f"manager: roster.json: entry 'bad': {why}\n"))

    def test_entry_name_must_match_name(self):
        for name in ("a b", "", "a;", "a\nb", "w/1"):
            with self.subTest(name=name):
                self.store(self.doc(entries={name: good()}))
                r, err = self.load()
                self.assertEqual((r["entries"], err), ({}, f"manager: roster.json: entry {name!r}: name: want [A-Za-z0-9_-]+\n"))

    def test_stderr_line_shows_values_by_repr(self):
        self.store(self.doc(entries={"bad": good(cwd="/w\n\x1b[31mforged ")}))
        err = self.load()[1]
        self.assertEqual(err.count("\n"), 1)
        self.assertNotIn("\x1b", err)
        self.assertIn(repr("/w\n\x1b[31mforged "), err)

    def test_later_write_drops_a_skipped_entry(self):
        self.store(self.doc(entries={"bad": good(sid="; rm -rf ~")}))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            manager.put(self.dir, "w1", good())
        self.assertEqual(list(self.entries()), ["w1"])
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_resume_with_shell_text_is_one_valid_word(self):
        resume = ["/usr/bin/python3", "/x/workers.py", "; rm -rf ~"]
        self.store(self.doc(entries={"w1": good(resume=resume)}))
        self.assertEqual(self.load()[0]["entries"]["w1"]["resume"], resume)

    def test_top_level_valid_forms(self):
        for top in ({}, {"holder": {"session": "s1", "since": WHEN}}, {"cursor": 7, "gen": 3},
                    {"holder": {"session": "s1", "since": "2026-01-01T00:00:00Z"}}):
            with self.subTest(top=top):
                self.store(self.doc(**top))
                self.assertEqual(self.load()[0], self.doc(**top))

    def test_top_level_faults_refuse_and_leave_the_bytes(self):
        for label, data, why in TOP:
            with self.subTest(label):
                top = data if isinstance(data, bytes) else {k: v for k, v in self.doc(**data).items() if v is not DROP}
                raw = self.store(top)
                for kw in ({}, {"write": False}):
                    with self.assertRaises(manager.ManagerError) as cm, manager.roster(self.dir, **kw):
                        self.fail("entered")
                    self.assertTrue(str(cm.exception).startswith(f"{self.path}: {why}"), str(cm.exception))
                    self.assertEqual(self.bytes(), raw)
                self.assertEqual(sorted(os.listdir(self.dir)), ["roster.json", "roster.lock"])

    def test_roster_json_symlink_refused_and_its_target_untouched(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        with open(target, "w") as f:
            json.dump(self.doc(), f)
        os.symlink(target, self.path)
        self.refused(f"{self.path}: not a regular file owned by you")
        self.refused(f"{self.path}: not a regular file owned by you", write=False)
        with open(target) as f:
            self.assertEqual(json.load(f), self.doc())
        self.assertTrue(os.path.islink(self.path))

    def test_roster_json_fifo_refused_without_blocking(self):
        os.mkfifo(self.path)
        with self.within():
            self.refused(f"{self.path}: not a regular file owned by you")
            self.refused(f"{self.path}: not a regular file owned by you", write=False)
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.path).st_mode))

    def test_roster_json_directory_refused(self):
        os.mkdir(self.path)
        self.refused(f"{self.path}: not a regular file owned by you")

    def test_roster_json_of_another_user_refused(self):
        raw = self.store(self.doc())
        with self.foreign(self.path):
            self.refused(f"{self.path}: not a regular file owned by you")
        self.assertEqual(self.bytes(), raw)

    def test_oversize_roster_json_refused_and_the_limit_itself_read(self):
        raw = self.store(b"x" * (manager.ROSTER_MAX + 1))
        self.refused(f"{self.path}: over {manager.ROSTER_MAX} bytes")
        self.assertEqual(self.bytes(), raw)
        body = json.dumps(self.doc()).encode()
        self.store(body + b" " * (manager.ROSTER_MAX - len(body)))
        self.assertEqual(os.path.getsize(self.path), manager.ROSTER_MAX)
        self.assertEqual(self.load()[0], self.doc())

    def test_roster_lock_symlink_refused_and_its_target_untouched(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        open(target, "w").close()
        os.symlink(target, self.lock)
        self.refused(f"{self.lock}: not a regular file owned by you")
        self.assertEqual(os.path.getsize(target), 0)
        self.assertFalse(os.path.exists(self.path))

    def test_roster_lock_of_another_user_refused(self):
        open(self.lock, "w").close()
        with self.foreign(self.lock):
            self.refused(f"{self.lock}: not a regular file owned by you")
        self.assertFalse(os.path.exists(self.path))

    def test_busy_lock_is_refused_after_the_timeout(self):
        self.hold()
        clock = types.SimpleNamespace(now=0.0, slept=[])

        def sleep(seconds):
            clock.slept.append(seconds)
            clock.now += seconds

        fake = types.SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
        with unittest.mock.patch.object(manager, "time", fake):
            self.refused(f"{self.lock}: busy")
        self.assertEqual(set(clock.slept), {0.1})
        self.assertAlmostEqual(sum(clock.slept), manager.LOCK_TIMEOUT, delta=0.2)
        self.assertFalse(os.path.exists(self.path))

    def test_two_processes_put_concurrently_and_both_entries_land(self):
        procs = [subprocess.Popen([sys.executable, "-I", "-c", CHILD, SRC, self.dir, tag], stderr=subprocess.PIPE, text=True)
                 for tag in "ab"]
        for p in procs:
            _, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual(set(self.entries()), {f"{t}{i}" for t in "ab" for i in range(15)})

    def test_missing_directory_is_not_attached_and_nothing_is_created(self):
        os.rmdir(self.dir)
        message = f"manager directory {self.dir} not attached: run workers.py attach first"
        self.refused(message)
        self.refused(message, write=False)
        self.assertEqual(os.listdir(self.managers), [])
        os.rmdir(self.managers)
        self.refused(message)
        self.assertEqual(os.listdir(self.agent_pm), [])

    def test_directory_open_to_group_or_other_refused_not_chmodded_and_nothing_created(self):
        os.chmod(self.dir, 0o750)
        with self.assertRaisesRegex(manager.ManagerError, f"^manager directory {self.dir}: .*group or other"), \
                manager.roster(self.dir):
            pass
        self.assertEqual(mode(self.dir), 0o750)
        self.assertEqual(os.listdir(self.dir), [])

    def test_directory_symlink_file_and_foreign_owner_refused(self):
        refused = f"manager directory {self.dir}: not a directory owned by you"
        real = os.lstat

        def lstat(path, *args, **kw):
            st = real(path, *args, **kw)
            return types.SimpleNamespace(st_mode=st.st_mode, st_uid=st.st_uid + 1) if path == self.dir else st

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

    def test_managers_of_another_user_refused(self):
        with unittest.mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.refused(f"manager directory {self.managers}: not a directory owned by you")

    def test_managers_symlink_refused_and_its_target_untouched(self):
        target = os.path.join(os.path.dirname(self.agent_pm), "target")
        os.rename(self.managers, target)
        os.symlink(target, self.managers)
        self.refused(f"manager directory {self.managers}: not a directory owned by you")
        self.assertEqual(os.listdir(os.path.join(target, "m1")), [])

    def test_entry_defaults_and_validity(self):
        e = manager.entry("worker", sid=SID, cwd="/w", resume=good()["resume"])
        self.assertEqual(sorted(e), sorted(good()))
        self.assertEqual({**e, "started": WHEN}, good())
        self.assertEqual(e["state"], "working")
        self.assertIsNotNone(datetime.datetime.fromisoformat(e["started"]).tzinfo)
        full = manager.entry("role", sid=None, cwd="/w", resume=["/p", "/x/drive.py"], note="n", opener="mgr", pane="%3",
                             split="right", split_from="a", tui="t", state="done", started=WHEN)
        self.assertEqual(full, good(kind="role", sid=None, resume=["/p", "/x/drive.py"], note="n", opener="mgr", pane="%3",
                                    split="right", split_from="a", tui="t", state="done"))
        with self.assertRaises(manager.ManagerError) as cm:
            manager.entry("boss", sid=SID, cwd="/w", resume=["/p", "/x/workers.py"])
        self.assertEqual(str(cm.exception), "entry: " + KIND.format("boss"))
        with self.assertRaisesRegex(manager.ManagerError, "^entry: cwd: 'rel': want"):
            manager.entry("worker", sid=SID, cwd="rel", resume=["/p", "/x/workers.py"])

    def test_set_entry_validates_replaces_and_leaves_the_dict_on_a_fault(self):
        with manager.roster(self.dir) as r:
            manager.set_entry(r, "w1", good())
            manager.set_entry(r, "w1", good(note="n"))
            with self.assertRaises(manager.ManagerError) as cm:
                manager.set_entry(r, "w2", good(sid="x"))
            self.assertEqual(str(cm.exception), "entry 'w2': sid: 'x': want a session id or null")
            with self.assertRaisesRegex(manager.ManagerError, "^entry 'a b': name: want"):
                manager.set_entry(r, "a b", good())
        self.assertEqual(self.entries(), {"w1": good(note="n")})

    def test_put_sets_one_entry_under_the_lock(self):
        manager.put(self.dir, "w1", good())
        manager.put(self.dir, "w2", good(state="done"))
        self.assertEqual(self.entries(), {"w1": good(), "w2": good(state="done")})
        with self.assertRaises(manager.ManagerError):
            manager.put(self.dir, "w3", good(kind="boss"))
        self.assertEqual(sorted(self.entries()), ["w1", "w2"])

    def test_record_keeps_the_note_of_a_replaced_entry_of_the_same_sid(self):
        cases = {"same sid, note None": (good(note="n"), good(state="done"), good(state="done", note="n")),
                 "new sid": (good(note="n"), good(sid=SID2), good(sid=SID2)),
                 "both sids null": (good(sid=None, note="n"), good(sid=None), good(sid=None)),
                 "a given note wins": (good(note="n"), good(note="m"), good(note="m")),
                 "no old entry": (None, good(), good())}
        for why, (old, value, want) in cases.items():
            with self.subTest(why):
                given = dict(value)
                with manager.roster(self.dir) as r:
                    r["entries"] = {} if old is None else {"w1": old}
                    manager.record(r, "w1", value)
                self.assertEqual(self.entries(), {"w1": want})
                self.assertEqual(value, given)

    def test_record_validates_and_leaves_the_old_entry_on_a_fault(self):
        with manager.roster(self.dir) as r:
            manager.set_entry(r, "w1", good(note="n"))
            with self.assertRaisesRegex(manager.ManagerError, "^entry 'w1': sid: 'x': want"):
                manager.record(r, "w1", good(sid="x"))
        self.assertEqual(self.entries(), {"w1": good(note="n")})

    def test_put_keeps_the_note_on_a_same_sid_replace(self):
        manager.put(self.dir, "w1", good(note="n"))
        manager.put(self.dir, "w1", good(state="done"))
        self.assertEqual(self.entries(), {"w1": good(state="done", note="n")})

    def test_unwritten_prints_the_one_failure_line_to_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            manager.unwritten("w1", manager.ManagerError("/d/roster.lock: busy"))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "manager: entry w1 not written: /d/roster.lock: busy\n")

    def test_remove_returns_the_entry_or_refuses_a_missing_one(self):
        with manager.roster(self.dir) as r:
            manager.set_entry(r, "w1", good())
        with manager.roster(self.dir) as r:
            self.assertEqual(manager.remove(r, "w1"), good())
            with self.assertRaises(manager.ManagerError) as cm:
                manager.remove(r, "w1")
            self.assertEqual(str(cm.exception), "w1 not in roster")
        self.assertEqual(self.entries(), {})

    def test_recovery_forms(self):
        resume = ["/usr/bin/python3", "/x/workers.py", "start"]
        for kind in ("worker", "pipeline"):
            with self.subTest(kind=kind):
                self.assertEqual(manager.recovery(good(kind=kind, resume=resume)), resume)
        self.assertEqual(manager.recovery(good(kind="worker", sid=None, resume=resume)), resume)
        self.assertEqual(manager.recovery(good(kind="role", resume=resume)),
                         [*resume, "--sid", SID, "--resume", "--input", "Continue the unfinished task."])
        self.assertIsNone(manager.recovery(good(kind="role", sid=None, resume=resume)))

    def test_recovery_is_a_copy(self):
        e = good(resume=["/p", "/x/workers.py"])
        manager.recovery(e).append("x")
        self.assertEqual(e["resume"], ["/p", "/x/workers.py"])

    def test_printable(self):
        for text in ("", "plain text", "café 日本", " ", "​", "a b", "~"):
            with self.subTest(text=text):
                self.assertIs(manager.printable(text), True)
        for text in ("\n", "\r", "\t", "\x00", "\x1b", "\x7f", "\x80", "\x85", "\x9f", " ", " ", "a\nb"):
            with self.subTest(text=text):
                self.assertIs(manager.printable(text), False)

    def test_sid(self):
        self.assertTrue(manager.SID.fullmatch(SID))
        for text in (SID.upper(), SID + "\n", SID[:-1], "", "; rm -rf ~", SID.replace("-", "")):
            with self.subTest(text=text):
                self.assertIsNone(manager.SID.fullmatch(text))

    def test_constants(self):
        self.assertEqual((manager.VERSION, manager.NOTE_MAX, manager.LOCK_TIMEOUT, manager.ROSTER_MAX),
                         (1, 500, 30, 1 << 20))
        self.assertEqual(manager.KINDS, ("worker", "role", "pipeline"))
        self.assertEqual(manager.STATES, ("working", "done", "blocked", "dead", "gone", "finished"))


NOW = "2026-02-02T02:02:02+00:00"
ATTACH = "manager directory {} not attached: run workers.py attach first"


def sessions(*live):
    """A proc answering tmux has-session with 0 for the sessions in `live`, else 1; its argv are recorded."""
    def proc(argv, **kw):
        proc.calls.append(argv)
        if argv[:3] != ["tmux", "has-session", "-t"]:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0 if argv[3] in [f"={s}" for s in live] else 1, "", "")

    proc.calls = []
    return proc


def no_tmux(argv, **kw):
    raise FileNotFoundError(2, "No such file or directory", "tmux")


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.agent_pm = hermetic.home(self)
        self.dir = os.path.join(self.agent_pm, "managers", "m1")
        self.path = os.path.join(self.dir, "roster.json")
        manager.ensure(self.dir)

    def doc(self, holder=None):
        return {"version": 1, "holder": holder, "cursor": 3, "gen": 1, "entries": {}}

    def held(self, session="other", since=WHEN):
        return {"session": session, "since": since}

    def message(self, session="other", since=WHEN):
        return (f"manager directory {self.dir}: held by tmux session {session} since {since}; "
                "ask that manager to run workers.py release, or end that session")

    def refused(self, call, r, message, kind=manager.ManagerError):
        before = json.dumps(r)
        with self.assertRaises(kind) as cm:
            call()
        self.assertIs(type(cm.exception), kind)
        self.assertEqual(str(cm.exception), message)
        self.assertEqual(json.dumps(r), before)

    def take(self, r, own, proc):
        with unittest.mock.patch.object(manager, "now", return_value=NOW):
            manager.take(r, self.dir, own, proc=proc)

    def test_held_is_a_manager_error(self):
        self.assertTrue(issubclass(manager.Held, manager.ManagerError))

    def test_take_without_a_holder_makes_own_the_holder(self):
        proc, r = sessions(), self.doc()
        self.take(r, "me", proc)
        self.assertEqual(r, self.doc(self.held("me", NOW)))
        self.assertEqual(proc.calls, [])

    def test_take_by_the_holder_keeps_since_and_asks_tmux_nothing(self):
        proc, r = sessions("me"), self.doc(self.held("me"))
        self.take(r, "me", proc)
        self.assertEqual(r, self.doc(self.held("me")))
        self.assertEqual(proc.calls, [])

    def test_take_from_a_live_other_is_held_with_the_fr13_text_and_changes_nothing(self):
        since = "2026-01-02T03:04:05+00:00"
        proc, r = sessions("other"), self.doc(self.held("other", since))
        self.refused(lambda: self.take(r, "me", proc), r, self.message("other", since), manager.Held)
        self.assertEqual(proc.calls, [["tmux", "has-session", "-t", "=other"]])

    def test_take_from_a_dead_other_takes_over_with_a_new_since(self):
        proc, r = sessions("me"), self.doc(self.held("other"))
        self.take(r, "me", proc)
        self.assertEqual(r, self.doc(self.held("me", NOW)))
        self.assertEqual(proc.calls, [["tmux", "has-session", "-t", "=other"]])

    def test_no_tmux_counts_as_not_live(self):
        r = self.doc(self.held("other"))
        self.take(r, "me", no_tmux)
        self.assertEqual(r["holder"], self.held("me", NOW))

    def test_take_outside_tmux_leaves_the_holder_untouched(self):
        for holder in (None, self.held("other")):
            with self.subTest(holder=holder):
                r = self.doc(holder)
                self.take(r, None, sessions())
                self.assertEqual(r, self.doc(holder))

    def test_take_outside_tmux_is_held_by_a_live_other(self):
        r = self.doc(self.held("other"))
        self.refused(lambda: self.take(r, None, sessions("other")), r, self.message(), manager.Held)

    def test_take_under_roster_writes_the_holder_and_a_refusal_writes_nothing(self):
        with manager.roster(self.dir) as r:
            manager.take(r, self.dir, "me", proc=sessions())
        with open(self.path, "rb") as f:
            raw = f.read()
        holder = json.loads(raw)["holder"]
        self.assertEqual(holder["session"], "me")
        self.assertIsNotNone(datetime.datetime.fromisoformat(holder["since"]).utcoffset())
        with self.assertRaises(manager.Held), manager.roster(self.dir) as r:
            manager.take(r, self.dir, "you", proc=sessions("me"))
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), raw)

    def test_check_passes_for_the_holder_and_asks_tmux_nothing(self):
        proc, r = sessions("me"), self.doc(self.held("me"))
        manager.check(r, self.dir, "me", proc=proc)
        self.assertEqual(r, self.doc(self.held("me")))
        self.assertEqual(proc.calls, [])

    def test_check_against_a_live_other_is_held_with_the_same_text_as_take(self):
        proc, r = sessions("other"), self.doc(self.held("other"))
        self.refused(lambda: manager.check(r, self.dir, "me", proc=proc), r, self.message(), manager.Held)
        self.assertEqual(proc.calls, [["tmux", "has-session", "-t", "=other"]])

    def test_check_by_a_non_holder_is_not_attached_and_takes_nothing(self):
        for holder, proc in ((None, sessions()), (self.held("other"), sessions()), (self.held("other"), no_tmux)):
            with self.subTest(holder=holder):
                r = self.doc(holder)
                self.refused(lambda: manager.check(r, self.dir, "me", proc=proc), r, ATTACH.format(self.dir))

    def test_check_outside_tmux_passes_without_a_live_holder(self):
        for holder in (None, self.held("other")):
            with self.subTest(holder=holder):
                r = self.doc(holder)
                manager.check(r, self.dir, None, proc=sessions())
                self.assertEqual(r, self.doc(holder))
        manager.check(self.doc(self.held("other")), self.dir, None, proc=no_tmux)

    def test_check_outside_tmux_is_held_by_a_live_other(self):
        r = self.doc(self.held("other"))
        self.refused(lambda: manager.check(r, self.dir, None, proc=sessions("other")), r, self.message(), manager.Held)

    def test_check_in_a_read_only_block_creates_no_roster_json(self):
        with manager.roster(self.dir, write=False) as r:
            manager.check(r, self.dir, None, proc=sessions())
            with self.assertRaises(manager.ManagerError):
                manager.check(r, self.dir, "me", proc=sessions())
        self.assertFalse(os.path.exists(self.path))

    def test_two_processes_take_concurrently_and_exactly_one_holds(self):
        procs = [subprocess.Popen([sys.executable, "-I", "-c", LEASE_CHILD, SRC, self.dir, own], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for own in ("a", "b")]
        for p in procs:
            p.stdin.write("\n")
            p.stdin.flush()   # communicate closes it: on 3.11 it flushes stdin first, so a closed one raises
        out = {}
        for own, p in zip("ab", procs):
            out[own] = p.communicate(timeout=60)
            self.assertIn(p.returncode, (0, 3), out[own][1])
        winners = [own for own, p in zip("ab", procs) if p.returncode == 0]
        self.assertEqual(len(winners), 1)
        loser = "b" if winners == ["a"] else "a"
        with open(self.path) as f:
            holder = json.load(f)["holder"]
        self.assertEqual(holder["session"], winners[0])
        self.assertEqual(out[loser][0].strip(), self.message(winners[0], holder["since"]))


if __name__ == "__main__":
    unittest.main()
