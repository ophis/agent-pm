"""modes/tui.py over tmux_host autospecced (no tmux runs) on a fake clock (time.monotonic and
time.sleep patched), with a temp HOME: the session dir and session.json, the name's lock,
read_state, begin, the hook records' tail, the pane scan, returncode, state, send, screen, close and
stop, the binding to its pane, and attach.
"""

import errno
import fcntl
import inspect
import json
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

from agent_pm.session.adapters.types import ACTIVITY, BLOCKED, TURN_END
from agent_pm.session.engine.lifecycle.types import Mode
from agent_pm.session.engine.types import (
    Activity,
    Blocked,
    DriverError,
    Key,
    LimitHit,
    Paste,
    SessionBound,
    SessionInfo,
    TurnEnd,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes.tui import types as tui

from session.tests import fake_adapter
from session.tests.case import SRC, Case

NAME = "w1"
SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
TMUX = "/opt/tmux/bin/tmux"
TS = "2026-10-11T12:00:00.000+00:00"
WORDS = {"stop": "done", "ask": "blocked", "tool": "working"}
DEFAULT = object()


def record(label: str, **payload) -> dict:
    return {"ts": TS, "label": label, "payload": payload}


def fake_host() -> mock.Mock:
    """tmux_host autospecced, its constants, TuiError and attach_command real: status None (absent),
    read "", option None, pane %1.
    """
    host = mock.create_autospec(tmux_host)
    for name in ("RUNNING", "UNKNOWN", "PAUSE", "TuiError"):
        setattr(host, name, getattr(tmux_host, name))
    host.attach_command.side_effect = tmux_host.attach_command
    host.status.return_value = None
    host.read.return_value = ""
    host.option.return_value = None
    host.pane.return_value = "%1"
    return host


class Clock:
    """time.monotonic and time.sleep's stand-in: sleep moves `now`, then runs each action due by
    then; every sleep is logged in `log`.
    """

    def __init__(self, log: list):
        self.now, self.log, self.actions = 0.0, log, []

    def sleep(self, seconds: float) -> None:
        self.log.append(("sleep", seconds))
        self.now += seconds
        while self.actions and self.actions[0][0] <= self.now:
            self.actions.pop(0)[1]()


class ScanTui(fake_adapter.FakeTui):
    """The fake Tui with two dialogs and a usage-limit message `LIMIT <epoch>`; activity is the
    payload's tool, the blocked reason its why; each screen limit() sees is kept.
    """

    dialogs = ("Trust  this\nfolder?", "Sign in")

    def __init__(self, adapter):
        super().__init__(adapter)
        self.screens = []

    def limit(self, screen):
        self.screens.append(screen)
        found = re.search(r"LIMIT ([0-9]+)", screen)
        return LimitHit(float(found[1])) if found else None

    def activity(self, payload):
        return payload.get("tool", "")

    def blocked_reason(self, payload):
        return payload.get("why", "")


class Base(Case):
    def setUp(self):
        self.root = os.path.join(self.agent_pm, "tui")
        self.dir = os.path.join(self.root, NAME)
        self.hooks = os.path.join(self.dir, "hooks.jsonl")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.events = os.path.join(self.tmp, "events")
        self.host = fake_host()
        self.log = []
        self.clock = Clock(self.log)
        for patch in (
            mock.patch("time.monotonic", lambda: self.clock.now),
            mock.patch("time.sleep", self.clock.sleep),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.run = ScanTui(fake_adapter.FakeAdapter({"tui": True}))
        self.info = SessionInfo(SID, "", self.tmp, False, "", "")

    def prepare(self, run=None, info=None, events=DEFAULT, workdir=None, tmux=TMUX) -> tui.TuiMode:
        events = self.events if events is DEFAULT else events
        return tui.TuiMode.prepare(
            NAME,
            run or self.run,
            info or self.info,
            events,
            runtime="fake",
            workdir=workdir or self.tmp,
            tmux=tmux,
            host=self.host,
        )

    def started(self) -> tui.TuiMode:
        """A TuiMode prepared (the session absent), begun on pane %1, the host's calls cleared, its
        status RUNNING.
        """
        self.host.status.return_value, self.host.pane.return_value = None, "%1"
        mode = self.prepare()
        mode.begin(("/bin/run",), cwd=self.tmp, env={})
        self.host.reset_mock()
        self.host.status.return_value = tmux_host.RUNNING
        return mode

    def append(self, *lines) -> None:
        """Each line to hooks.jsonl: bytes as given, else a JSON line."""
        with open(self.hooks, "ab") as f:
            for line in lines:
                f.write(line if isinstance(line, bytes) else (json.dumps(line) + "\n").encode())

    def state(self, **changes) -> dict:
        return {
            "runtime": "fake",
            "sid": SID,
            "workdir": self.tmp,
            "cwd": self.tmp,
            "project": False,
            "transcript": "",
            "resume": "",
            "events": self.events,
            "words": WORDS,
            "tmux_path": TMUX,
            **changes,
        }

    def write_state(self, value) -> None:
        os.makedirs(self.dir, 0o700, exist_ok=True)
        with open(os.path.join(self.dir, "session.json"), "wb") as f:
            f.write(value if isinstance(value, bytes) else json.dumps(value).encode())

    def assertNoSessionDir(self):
        self.assertFalse(os.path.lexists(self.dir))

    def locked(self, name: str = NAME) -> bool:
        try:
            fd = os.open(os.path.join(self.root, f"{name}.lock"), os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False


class PathsTest(Base):
    def test_session_dir_is_under_tui_dir_in_home(self):
        self.assertEqual(tui.TUI_DIR, "~/.agent-pm/tui")
        self.assertEqual(tui.session_dir(NAME), self.dir)

    def test_hook_argv_runs_tui_hook_isolated_on_the_dir(self):
        hook = os.path.join(SRC["session"], "agent_pm", "session", "modes", "tui_hook.py")
        argv = tui.hook_argv("/d/w1")
        self.assertEqual(argv, (sys.executable, "-I", argv[2], "/d/w1"))
        self.assertTrue(os.path.isabs(argv[2]))
        self.assertTrue(os.path.samefile(argv[2], hook))


class PrepareTest(Base):
    def test_writes_session_json_0600_in_a_0700_session_dir(self):
        info = SessionInfo(SID, "", "/cwd", True, "/t/x.jsonl", "fake --resume x")
        mode = self.prepare(info=info, workdir="/work")
        with open(os.path.join(self.dir, "session.json")) as f:
            self.assertEqual(
                json.load(f),
                self.state(
                    workdir="/work",
                    cwd="/cwd",
                    project=True,
                    transcript="/t/x.jsonl",
                    resume="fake --resume x",
                ),
            )
        self.assertEqual(os.listdir(self.dir), ["session.json"])
        for path, mode_bits in (
            (self.root, 0o700),
            (self.dir, 0o700),
            (os.path.join(self.dir, "session.json"), 0o600),
        ):
            self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), mode_bits, path)
        self.assertEqual(self.host.mock_calls, [mock.call.status(NAME)])
        self.assertEqual(
            (mode.name, mode.sid, mode.events, mode.dir), (NAME, SID, self.events, self.dir)
        )

    def test_only_labels_of_a_known_kind_get_a_word_and_events_may_be_none(self):
        class Labels(ScanTui):
            hooks = {
                "end": TURN_END,
                "perm": BLOCKED,
                "note": BLOCKED,
                "use": ACTIVITY,
                "odd": "other",
            }

        self.prepare(run=Labels(self.run.adapter), events=None)
        self.assertEqual(
            tui.read_state(NAME),
            self.state(
                events=None,
                words={"end": "done", "perm": "blocked", "note": "blocked", "use": "working"},
            ),
        )

    def test_a_dead_or_absent_session_is_prepared(self):
        for status in (0, 2, -15, None):
            with self.subTest(status=status):
                self.host.status.return_value = status
                self.prepare()
                self.assertTrue(os.path.isfile(os.path.join(self.dir, "session.json")))

    def test_a_running_or_unknown_session_is_refused_with_nothing_made(self):
        for status, message in (
            (tmux_host.RUNNING, "tui session w1 is running; tmux attach -t '=w1'"),
            (tmux_host.UNKNOWN, "tui session w1: its tmux state is unknown"),
        ):
            with self.subTest(status=status):
                self.host.status.return_value = status
                with self.assertRaises(DriverError) as caught:
                    self.prepare()
                self.assertEqual(str(caught.exception), message)
                self.assertFalse(os.path.lexists(self.root))

    def test_a_bad_session_state_is_refused_before_tmux_or_a_path(self):
        class BadLabel(ScanTui):
            hooks = {"a b": TURN_END}

        for kwargs, field in (
            ({"info": SessionInfo(SID, "", self.tmp, False, "rel.jsonl", "")}, "transcript"),
            ({"info": SessionInfo(SID, "", "rel", False, "", "")}, "cwd"),
            ({"info": SessionInfo("a/b", "", self.tmp, False, "", "")}, "sid"),
            ({"events": "rel"}, "events"),
            ({"workdir": "/a\nb"}, "workdir"),
            ({"run": BadLabel(self.run.adapter)}, "words"),
            ({"tmux": "bin/tmux"}, "tmux_path"),
            ({"tmux": None}, "tmux_path"),
        ):
            with self.subTest(field):
                with self.assertRaisesRegex(DriverError, rf"\Atui session w1: bad {field} "):
                    self.prepare(**kwargs)
        self.assertEqual(self.host.mock_calls, [])
        self.assertFalse(os.path.lexists(self.root))

    def test_tui_dir_and_home_dir_are_made_when_missing(self):
        os.rmdir(self.agent_pm)
        self.prepare()
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)

    def test_an_existing_tui_dir_is_set_to_0700(self):
        os.mkdir(self.root, 0o755)
        os.chmod(self.root, 0o755)
        self.prepare()
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)

    def test_a_tui_dir_that_is_no_real_directory_of_yours_is_refused(self):
        target = os.path.join(self.tmp, "target")
        os.mkdir(target, 0o755)
        os.chmod(target, 0o755)
        cases = (
            ("symlink", lambda: os.symlink(target, self.root), None),
            ("file", lambda: open(self.root, "w").close(), None),
            ("another user's", lambda: os.mkdir(self.root, 0o700), os.getuid() + 1),
        )
        for case, make, uid in cases:
            with self.subTest(case):
                make()
                with mock.patch("os.getuid", return_value=uid if uid is not None else os.getuid()):
                    with self.assertRaisesRegex(DriverError, re.escape(self.root)):
                        self.prepare()
                self.assertTrue(os.path.lexists(self.root))
                (os.rmdir if case == "another user's" else os.unlink)(self.root)
        self.assertEqual((os.listdir(target), stat.S_IMODE(os.stat(target).st_mode)), ([], 0o755))
        self.host.start.assert_not_called()

    def test_an_old_session_dir_is_replaced(self):
        os.makedirs(os.path.join(self.dir, "sub"))
        for name in ("hooks.jsonl", "session.json", "other"):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write("old")
        self.prepare()
        self.assertEqual(os.listdir(self.dir), ["session.json"])
        self.assertEqual(tui.read_state(NAME), self.state())

    def test_an_old_entry_that_is_no_real_directory_is_refused(self):
        os.mkdir(self.root, 0o700)
        target = os.path.join(self.tmp, "target")
        os.mkdir(target)
        open(os.path.join(target, "keep"), "w").close()
        for case, make in (
            ("symlink", lambda: os.symlink(target, self.dir)),
            ("file", lambda: open(self.dir, "w").close()),
            ("fifo", lambda: os.mkfifo(self.dir)),
        ):
            with self.subTest(case):
                make()
                with self.assertRaisesRegex(
                    DriverError, rf"\A{re.escape(self.dir)}: not a directory\Z"
                ):
                    self.prepare()
                self.assertTrue(os.path.lexists(self.dir))
                os.unlink(self.dir)
        self.assertEqual(os.listdir(target), ["keep"])

    def test_a_session_dir_another_start_makes_first_is_starting_and_kept(self):
        real = os.mkdir

        def racing(path, mode=0o777, **kwargs):
            if os.fspath(path) == self.dir:  # another start's mkdir lands first
                real(path, 0o700)
                open(os.path.join(path, "theirs"), "w").close()
            return real(path, mode, **kwargs)

        with mock.patch("os.mkdir", racing), self.assertRaises(DriverError) as caught:
            self.prepare()
        self.assertEqual(str(caught.exception), "tui session w1 is starting")
        self.assertEqual(os.listdir(self.dir), ["theirs"])

    def test_a_failed_write_is_a_driver_error_and_removes_the_dir(self):
        with (
            mock.patch(
                "tempfile.mkstemp", side_effect=OSError(errno.ENOSPC, "No space left on device")
            ),
            self.assertRaisesRegex(DriverError, "No space left on device"),
        ):
            self.prepare()
        self.assertNoSessionDir()
        self.assertTrue(os.path.isdir(self.root))


class LockTest(Base):
    def test_a_0600_lock_file_per_name_in_tui_dir_held_till_the_block_ends(self):
        os.rmdir(self.agent_pm)
        with tui.lock(NAME):
            self.assertTrue(self.locked())
            with tui.lock("w2"):
                self.assertTrue(self.locked("w2"))
            self.assertFalse(self.locked("w2"))
        self.assertFalse(self.locked())
        self.assertEqual(stat.S_IMODE(os.lstat(os.path.join(self.root, "w1.lock")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)
        self.assertEqual(self.host.mock_calls, [])

    def test_a_second_holder_waits_for_the_first(self):
        entered = threading.Event()

        def second():
            with tui.lock(NAME):
                entered.set()

        with tui.lock(NAME):
            thread = threading.Thread(target=second)
            thread.start()
            self.assertFalse(entered.wait(0.2))
        thread.join(5)
        self.assertTrue(entered.is_set())

    def test_a_lock_path_that_is_no_regular_file_is_refused_at_once(self):
        os.mkdir(self.root, 0o700)
        target = os.path.join(self.tmp, "target")
        open(target, "w").close()
        path = os.path.join(self.root, "w1.lock")
        for case, make in (
            ("symlink", lambda: os.symlink(target, path)),
            ("fifo", lambda: os.mkfifo(path)),
            ("directory", lambda: os.mkdir(path)),
        ):
            with self.subTest(case):
                make()
                with self.assertRaisesRegex(DriverError, rf"\A{re.escape(path)}: "), tui.lock(NAME):
                    pass
                (os.rmdir if case == "directory" else os.unlink)(path)


class BeginTest(Base):
    def test_starts_the_session_tagged_with_its_sid_and_runtime(self):
        mode = self.prepare()
        mode.begin(["/bin/run", "a"], cwd=self.tmp, env={"A": "1"}, status_line=True)
        self.assertEqual(
            self.host.mock_calls[1:],
            [
                mock.call.start(
                    NAME,
                    ["/bin/run", "a"],
                    cwd=self.tmp,
                    env={"A": "1"},
                    events=self.events,
                    status_line=True,
                    tags={"@sid": SID, "@runtime": "fake"},
                ),
                mock.call.pane(NAME),
            ],
        )
        self.prepare(events=None).begin(("/bin/run",), cwd=self.tmp, env={})
        self.assertEqual(
            self.host.start.call_args,
            mock.call(
                NAME,
                ["/bin/run"],
                cwd=self.tmp,
                env={},
                events=None,
                status_line=False,
                tags={"@sid": SID, "@runtime": "fake"},
            ),
        )

    def test_a_failed_start_is_a_driver_error_removing_only_its_own_dir(self):
        other = os.path.join(self.root, "w2")
        os.makedirs(other)
        mode = self.prepare()
        self.host.start.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(DriverError) as caught:
            mode.begin(("/bin/run",), cwd=self.tmp, env={})
        self.assertEqual(str(caught.exception), "cannot start tui session w1: tmux: boom")
        self.assertNoSessionDir()
        self.assertEqual(os.listdir(self.root), ["w2"])
        mode.stop()
        self.host.kill.assert_not_called()

    def test_a_failed_start_keeps_a_dir_it_did_not_make(self):
        mode = self.prepare()
        os.rename(
            self.dir, os.path.join(self.tmp, "ours")
        )  # kept, so the new dir cannot reuse its inode
        os.mkdir(self.dir)
        open(os.path.join(self.dir, "theirs"), "w").close()
        self.host.start.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(DriverError):
            mode.begin(("/bin/run",), cwd=self.tmp, env={})
        self.assertEqual(os.listdir(self.dir), ["theirs"])

    def test_a_pane_it_cannot_take_closes_the_session(self):
        mode = self.prepare()
        self.host.pane.side_effect = tmux_host.TuiError("tmux: boom")
        with self.assertRaises(DriverError) as caught:
            mode.begin(("/bin/run",), cwd=self.tmp, env={})
        self.assertEqual(str(caught.exception), "cannot start tui session w1: tmux: boom")
        self.host.kill.assert_called_once_with(NAME)
        self.assertNoSessionDir()

    def test_a_pane_it_cannot_take_under_the_names_lock_closes_without_waiting_for_the_lock(self):
        mode, caught = self.prepare(), []
        self.host.pane.side_effect = tmux_host.TuiError("tmux: boom")

        def begin():
            with tui.lock(NAME):
                try:
                    mode.begin(("/bin/run",), cwd=self.tmp, env={})
                except DriverError as e:
                    caught.append(str(e))

        thread = threading.Thread(target=begin, daemon=True)
        thread.start()
        thread.join(5)
        self.assertEqual(caught, ["cannot start tui session w1: tmux: boom"])
        self.host.kill.assert_called_once_with(NAME)
        self.assertNoSessionDir()


class ReadStateTest(Base):
    def test_a_valid_state(self):
        self.prepare()
        self.assertEqual(tui.read_state(NAME), self.state())
        self.write_state(
            self.state(
                events=None, cwd="", transcript="/t", project=True, resume="r", words={}, later=1
            )
        )
        self.assertEqual(tui.read_state(NAME)["later"], 1)

    def test_missing_or_invalid_is_a_driver_error(self):
        with self.assertRaisesRegex(DriverError, r"\Ano tui session w1: "):
            tui.read_state(NAME)
        bad = {
            "words": {"stop": "dead"},
            "events": "rel",
            "sid": "a/b",
            "runtime": "Fake",
            "workdir": "rel",
            "cwd": "rel",
            "transcript": "rel",
            "project": "no",
            "resume": None,
            "tmux_path": "tmux",
        }
        for field, value in bad.items():
            for state in (
                self.state(**{field: value}),
                {k: v for k, v in self.state().items() if k != field},
            ):
                with self.subTest(field=field, missing=field not in state):
                    self.write_state(state)
                    with self.assertRaisesRegex(
                        DriverError, rf"\Ano tui session w1: .*bad {field}"
                    ):
                        tui.read_state(NAME)
        for data in (b"", b"[]", b"not json"):
            with self.subTest(data):
                self.write_state(data)
                with self.assertRaisesRegex(DriverError, r"\Ano tui session w1: "):
                    tui.read_state(NAME)

    def test_a_session_json_that_is_no_regular_file_is_refused_without_blocking(self):
        path = os.path.join(self.dir, "session.json")
        os.makedirs(self.dir)
        real = os.path.join(self.tmp, "real.json")
        with open(real, "w") as f:
            json.dump(self.state(), f)
        for make in (
            lambda: os.symlink(real, path),
            lambda: os.mkfifo(path),
            lambda: os.mkdir(path),
        ):
            make()
            with self.subTest(stat.filemode(os.lstat(path).st_mode)):
                out = {}
                thread = threading.Thread(
                    target=lambda *, out=out: out.setdefault("error", self.refused()), daemon=True
                )
                thread.start()
                thread.join(10)
                self.assertFalse(thread.is_alive(), "blocked")
                self.assertTrue(out["error"])
            (os.rmdir if stat.S_ISDIR(os.lstat(path).st_mode) else os.unlink)(path)

    def refused(self) -> bool:
        try:
            tui.read_state(NAME)
        except DriverError:
            return True
        return False


class PollTest(Base):
    def test_records_become_the_runs_events_in_order_bound_once(self):
        mode = self.started()
        self.append(
            record("tool", session_id="n1", tool="Bash"),
            record("stop", session_id="n1", last_message="hi", tasks=[1, 2]),
        )
        self.assertEqual(
            mode.poll(0.5), ([SessionBound("n1"), Activity("Bash"), TurnEnd("hi", 2)], False)
        )
        self.append(
            record("ask", session_id="n2", why="perm"),
            record("other", session_id="n2"),
            record("tool"),
        )
        self.assertEqual(
            mode.poll(0.5), ([SessionBound("n2"), Blocked("perm"), Activity("")], False)
        )
        self.assertEqual(mode.bound, "n2")
        self.assertEqual(self.host.mock_calls, [mock.call.pane(NAME)] * 2)
        self.assertEqual(self.log, [])

    def test_a_partial_last_line_waits_for_its_end(self):
        mode = self.started()
        line = (json.dumps(record("stop", last_message="r")) + "\n").encode()
        self.append(record("tool"), line[:10])
        self.assertEqual(mode.poll(0), ([Activity("")], False))
        self.assertEqual(mode.poll(0), ([], False))
        self.append(line[10:])
        self.assertEqual(mode.poll(0), ([TurnEnd("r", 0)], False))

    def test_bad_lines_are_skipped(self):
        mode = self.started()
        self.append(
            b"not json\n",
            b"[1]\n",
            b'{"label": 1, "payload": {}}\n',
            b'{"label": "stop", "payload": []}\n',
            b'{"label": "stop"}\n',
            b"\xff\xfe\n",
            b"\n",
            b"[" * 100000 + b"\n",
            record("tool", tool="t"),
        )
        self.assertEqual(mode.poll(0), ([Activity("t")], False))

    def test_a_hooks_file_that_is_no_regular_file_gives_no_records(self):
        mode = self.started()
        real = os.path.join(self.tmp, "real.jsonl")
        with open(real, "w") as f:
            f.write(json.dumps(record("tool")) + "\n")
        for make in (lambda: os.symlink(real, self.hooks), lambda: os.mkfifo(self.hooks)):
            make()
            with self.subTest(stat.filemode(os.lstat(self.hooks).st_mode)):
                self.assertEqual(mode.poll(0), ([], False))
            os.unlink(self.hooks)

    def test_nothing_new_checks_status_the_screen_then_its_pane_once_then_waits_the_timeout_in_slices(
        self,
    ):
        mode = self.started()
        self.assertEqual(mode.poll(0.5), ([], False))
        self.assertEqual(
            self.host.mock_calls,
            [mock.call.status(NAME), mock.call.read("%1"), mock.call.pane(NAME)],
        )
        self.assertTrue(all(0 < s <= 0.1 for _, s in self.log), self.log)
        self.assertAlmostEqual(self.clock.now, 0.5)

    def test_a_record_while_waiting_returns_at_once(self):
        mode = self.started()
        self.clock.actions.append((0.25, lambda: self.append(record("tool", tool="late"))))
        self.assertEqual(mode.poll(5), ([Activity("late")], False))
        self.assertAlmostEqual(self.clock.now, 0.3)

    def test_dead_or_absent_drains_the_records_then_ends(self):
        for status, code in ((3, 3), (-15, -15), (0, 0), (None, -signal.SIGHUP)):
            with self.subTest(status=status):
                mode = self.started()

                def ended(name, status=status):
                    self.append(record("stop", last_message="last"))  # written before the pane died
                    return status

                self.host.status.side_effect = ended
                self.assertEqual(mode.poll(0.5), ([TurnEnd("last", 0)], True))
                self.host.read.assert_not_called()
                self.host.status.side_effect = AssertionError("status after the end")
                self.assertEqual(mode.returncode(1), code)
                self.host.status.side_effect = None

    def test_unknown_keeps_waiting_with_no_scan(self):
        mode = self.started()
        self.host.status.return_value = tmux_host.UNKNOWN
        self.assertEqual(mode.poll(0.2), ([], False))
        self.host.read.assert_not_called()

    def test_exact_events_per_poll_over_a_session(self):
        mode = self.started()
        self.host.read.side_effect = ["", "Sign in", "Sign in", "x", "LIMIT 9", "x"]
        script = [
            [record("tool", session_id="n1")],
            [],
            [],
            [record("ask", session_id="n1")],
            [],
            [],
            [record("stop", session_id="n1", last_message="done")],
        ]
        got = []
        for lines in script:
            self.append(*lines)
            got.append(mode.poll(0.1))
        self.assertEqual(
            got,
            [
                ([SessionBound("n1"), Activity("")], False),
                ([], False),
                ([Blocked("Sign in")], False),
                ([Blocked("")], False),
                ([], False),
                ([Activity("")], False),
                ([TurnEnd("done", 0)], False),
            ],
        )
        self.assertEqual(mode.poll(0.1), ([LimitHit(9.0)], False))
        self.assertEqual(mode.poll(0.1), ([], False))


class ScanTest(Base):
    def scans(self, mode, *screens) -> list:
        """mode.poll's events for each screen in turn."""
        found = []
        for screen in screens:
            self.host.read.return_value = screen
            found.append(mode.poll(0)[0])
        return found

    def test_a_dialog_is_blocked_once_and_its_end_activity(self):
        mode = self.started()
        self.assertEqual(
            self.scans(mode, "x", "a Trust this \n  folder? b", "Trust this folder?", "y", "y"),
            [[], [Blocked("Trust  this\nfolder?")], [], [Activity("")], []],
        )
        self.assertEqual(self.run.screens[1], "a Trust this folder? b")

    def test_two_dialogs_one_going_as_the_other_comes(self):
        mode = self.started()
        self.assertEqual(
            self.scans(mode, "Sign in; Trust this folder?", "Sign in", "Trust this folder?", ""),
            [
                [Blocked("Trust  this\nfolder?"), Blocked("Sign in")],
                [Activity("")],
                [Activity(""), Blocked("Trust  this\nfolder?")],
                [Activity("")],
            ],
        )

    def test_a_dialog_marks_the_pane_blocked_unless_it_is(self):
        mode = self.started()
        self.host.option.return_value = "done"
        self.scans(mode, "Sign in Trust this folder?")
        self.assertEqual(
            self.host.mock_calls[3:],
            [mock.call.option("%1", "@state"), mock.call.mark("blocked", self.events, pane="%1")],
        )
        self.host.reset_mock()
        self.host.option.return_value = "blocked"
        self.scans(mode, "", "Sign in")
        self.host.mark.assert_not_called()

    def test_a_name_with_no_pane_is_absent_unscanned(self):
        mode = self.started()
        self.host.pane.return_value = None
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual((self.run.screens, mode.returncode(0)), ([], -signal.SIGHUP))
        self.host.mark.assert_not_called()

    def test_a_failed_mark_still_yields_the_dialog(self):
        mode = self.started()
        for method in ("option", "mark"):
            with self.subTest(method):
                getattr(self.host, method).side_effect = tmux_host.TuiError("tmux: boom")
                self.assertEqual(
                    self.scans(mode, "Sign in", ""), [[Blocked("Sign in")], [Activity("")]]
                )
                getattr(self.host, method).side_effect = None

    def test_a_limit_is_one_event_per_appearance_its_reset_changing_or_not(self):
        mode = self.started()
        self.assertEqual(
            self.scans(mode, "LIMIT 100", "LIMIT  100", "LIMIT 200", "", "LIMIT 200", "LIMIT 300"),
            [[LimitHit(100.0)], [], [], [], [LimitHit(200.0)], []],
        )

    def test_a_failed_read_is_no_event(self):
        mode = self.started()
        self.host.read.side_effect = tmux_host.TuiError("tmux: gone")
        self.assertEqual(mode.poll(0), ([], False))


class ReturncodeStateTest(Base):
    def test_returncode_maps_the_status(self):
        for status, code in (
            (0, 0),
            (2, 2),
            (-9, -9),
            (None, -signal.SIGHUP),
            (tmux_host.RUNNING, None),
            (tmux_host.UNKNOWN, None),
        ):
            with self.subTest(status=status):
                mode = self.started()
                self.host.status.return_value = status
                self.assertEqual(mode.returncode(0), code)

    def test_returncode_waits_its_timeout_in_slices_while_running(self):
        mode = self.started()
        self.clock.actions.append((0.25, lambda: setattr(self.host.status, "return_value", 4)))
        self.assertEqual(mode.returncode(5), 4)
        self.assertAlmostEqual(self.clock.now, 0.3)
        self.host.status.return_value = tmux_host.RUNNING
        mode = self.started()
        self.clock.now = 0
        self.assertIsNone(mode.returncode(0.35))
        self.assertAlmostEqual(self.clock.now, 0.35)

    def test_state_maps_the_status(self):
        for status, state in (
            (tmux_host.RUNNING, "live"),
            (0, "live"),
            (-15, "live"),
            (None, "absent"),
            (tmux_host.UNKNOWN, "unknown"),
        ):
            with self.subTest(status=status):
                mode = self.started()
                self.host.status.return_value = status
                self.assertEqual(mode.state(), state)


class Keys(ScanTui):
    def submit(self, text):
        return Key("Escape"), Paste(text), Key("Enter"), Key("Enter"), Paste("b"), Key("Tab")


class SendScreenTest(Base):
    def test_send_pastes_and_presses_the_runs_keys_waiting_pause_after_a_paste(self):
        mode = self.started()
        self.host.paste.side_effect = lambda *args: self.log.append(("paste", *args))
        self.host.keys.side_effect = lambda *args: self.log.append(("keys", *args))
        mode.send("hi\nthere")
        self.assertEqual(
            self.log,
            [("paste", "%1", "hi\nthere"), ("sleep", tmux_host.PAUSE), ("keys", "%1", "Enter")],
        )
        self.log.clear()
        mode.run = Keys(self.run.adapter)
        mode.send("a")
        self.assertEqual(
            self.log,
            [
                ("keys", "%1", "Escape"),
                ("paste", "%1", "a"),
                ("sleep", tmux_host.PAUSE),
                ("keys", "%1", "Enter"),
                ("keys", "%1", "Enter"),
                ("paste", "%1", "b"),
                ("sleep", tmux_host.PAUSE),
                ("keys", "%1", "Tab"),
            ],
        )

    def test_send_or_screen_to_a_session_not_running_is_refused(self):
        mode = self.started()
        for status in (0, -9, None, tmux_host.UNKNOWN):
            for call in (lambda: mode.send("hi"), mode.screen):
                with self.subTest(status=status):
                    self.host.status.return_value = status
                    with self.assertRaises(DriverError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), "tui session w1 is not running")
        self.host.paste.assert_not_called()
        self.host.read.assert_not_called()

    def test_a_tmux_error_is_a_driver_error(self):
        mode = self.started()
        for method, call in (
            ("paste", lambda: mode.send("hi")),
            ("keys", lambda: mode.send("hi")),
            ("read", mode.screen),
        ):
            with self.subTest(method):
                getattr(self.host, method).side_effect = tmux_host.TuiError("tmux: boom")
                with self.assertRaisesRegex(DriverError, r"\A.*tui session w1: tmux: boom\Z"):
                    call()
                getattr(self.host, method).side_effect = None

    def test_screen_reads_the_pane(self):
        mode = self.started()
        self.host.read.return_value = "a\nb"
        self.assertEqual((mode.screen(), mode.screen(5)), ("a\nb", "a\nb"))
        self.assertEqual(self.host.read.call_args_list, [mock.call("%1", None), mock.call("%1", 5)])


class CloseStopTest(Base):
    def test_close_kills_the_session_and_removes_its_dir(self):
        self.started()
        self.append(record("tool"))
        tui.close(NAME, host=self.host)
        self.assertEqual(self.host.mock_calls, [mock.call.kill(NAME)])
        self.assertNoSessionDir()
        self.assertTrue(os.path.isdir(self.root))

    def test_close_kills_and_removes_under_the_names_lock(self):
        self.started()
        held, remove = [], shutil.rmtree
        self.host.kill.side_effect = lambda name: held.append(self.locked())
        with mock.patch.object(
            shutil, "rmtree", lambda path: (held.append(self.locked()), remove(path))
        ):
            tui.close(NAME, host=self.host)
        self.assertEqual((held, self.locked()), ([True, True], False))
        self.assertNoSessionDir()

    def test_close_of_an_absent_session_removes_its_dir(self):
        self.started()
        self.host.kill.side_effect = tmux_host.TuiError("tmux: can't find session: w1")
        self.host.status.return_value = None
        tui.close(NAME, host=self.host)
        self.assertNoSessionDir()
        tui.close(NAME, host=self.host)

    def test_a_kill_that_fails_on_a_live_or_unknown_session_is_a_driver_error(self):
        self.started()
        self.host.kill.side_effect = tmux_host.TuiError("tmux: boom")
        for status in (tmux_host.RUNNING, tmux_host.UNKNOWN, 0):
            with self.subTest(status=status):
                self.host.status.return_value = status
                with self.assertRaises(DriverError) as caught:
                    tui.close(NAME, host=self.host)
                self.assertEqual(str(caught.exception), "cannot close tui session w1: tmux: boom")
                self.assertTrue(os.path.isdir(self.dir))

    def test_close_never_follows_a_symlink(self):
        os.mkdir(self.root, 0o700)
        target = os.path.join(self.tmp, "target")
        os.mkdir(target)
        open(os.path.join(target, "keep"), "w").close()
        for make in (lambda: os.symlink(target, self.dir), lambda: open(self.dir, "w").close()):
            make()
            tui.close(NAME, host=self.host)
            self.assertTrue(os.path.lexists(self.dir))
            os.unlink(self.dir)
        self.assertEqual(os.listdir(target), ["keep"])

    def test_stop_closes_a_begun_session_only(self):
        mode = self.prepare()
        mode.stop()
        self.assertEqual(self.host.mock_calls, [mock.call.status(NAME)])
        mode.begin(("/bin/run",), cwd=self.tmp, env={})
        mode.stop()
        self.host.kill.assert_called_once_with("%1")
        self.assertNoSessionDir()


class BoundTest(Base):
    """A begun Mode is its pane's (%1): another pane under its name, or stop(), ends it."""

    def test_another_pane_under_the_name_ends_it_at_once_unscanned_unmarked_and_unread(self):
        for status in (tmux_host.RUNNING, 3):
            with self.subTest(status=status):
                mode = self.started()
                self.host.status.return_value, self.host.pane.return_value = status, "%2"
                self.host.read.return_value = "Sign in LIMIT 9"
                self.log.clear()
                self.assertEqual(mode.poll(5), ([], True))
                self.assertEqual(
                    (self.run.screens, self.log, mode.returncode(0)), ([], [], -signal.SIGHUP)
                )
                self.host.mark.assert_not_called()
                self.append(record("tool"))
                self.host.reset_mock()
                self.assertEqual(mode.poll(5), ([], True))
                self.assertEqual(self.host.mock_calls, [])

    def test_records_under_another_pane_are_never_read(self):
        mode = self.started()
        self.host.pane.return_value = "%2"
        self.append(record("stop", last_message="theirs"))
        self.assertEqual(mode.poll(5), ([], True))
        mode = self.started()

        def theirs():
            self.host.pane.return_value = "%2"
            self.append(record("stop", last_message="theirs"))

        self.clock.actions.append((self.clock.now + 0.25, theirs))
        self.assertEqual((mode.poll(5), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_stop_ends_it_at_once_its_code_the_one_seen_else_sighup(self):
        mode = self.started()
        mode.stop()
        os.mkdir(self.dir)
        self.append(record("tool"))
        self.host.reset_mock()
        self.assertEqual((mode.poll(5), mode.returncode(5)), (([], True), -signal.SIGHUP))
        self.assertEqual((self.host.mock_calls, self.log), ([], []))
        mode = self.started()
        self.host.status.return_value = 3
        self.assertEqual(mode.poll(0), ([], True))
        mode.stop()
        self.assertEqual(mode.returncode(0), 3)

    def test_stop_kills_its_pane_while_it_holds_the_name_the_name_when_tmux_fails(self):
        for holder, kills, removed in (
            ("%1", [mock.call("%1")], True),
            ("%2", [], False),
            (tmux_host.TuiError("tmux: boom"), [mock.call(NAME)], True),
        ):
            with self.subTest(holder=holder):
                mode = self.started()
                self.host.pane.return_value = holder
                self.host.pane.side_effect = holder if isinstance(holder, Exception) else None
                mode.stop()
                self.assertEqual(
                    (self.host.kill.call_args_list, os.path.isdir(self.dir)), (kills, not removed)
                )
                self.assertEqual(mode.poll(0), ([], True))
                self.host.pane.side_effect = None

    def test_stop_with_the_name_absent_kills_nothing_as_a_new_server_may_reuse_its_pane_id(self):
        mode = self.started()
        self.host.pane.return_value = None
        self.host.status.side_effect = lambda target: tmux_host.RUNNING if target == "%1" else None
        mode.stop()
        self.assertEqual(self.host.mock_calls, [mock.call.pane(NAME)])
        self.assertNoSessionDir()
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_stop_checks_the_holder_and_closes_under_the_names_lock(self):
        held = []
        for holder, closed in (("%1", True), ("%2", False)):
            with self.subTest(holder=holder):
                self.host.pane.side_effect = self.host.kill.side_effect = None
                mode = self.started()
                held.clear()
                self.host.pane.side_effect = lambda name, *, holder=holder: (
                    held.append(self.locked()) or holder
                )
                self.host.kill.side_effect = lambda name: held.append(self.locked())
                mode.stop()
                self.assertEqual((held, self.locked()), ([True] * (1 + closed), False))

    def test_another_pane_under_the_name_is_absent_to_state_send_and_screen(self):
        mode = self.started()
        self.host.pane.return_value = "%2"
        self.assertEqual(mode.state(), "absent")
        for call in (lambda: mode.send("hi"), mode.screen):
            with self.assertRaisesRegex(DriverError, r"\Atui session w1 is not running\Z"):
                call()
        self.host.paste.assert_not_called()
        self.host.read.assert_not_called()
        self.assertEqual((mode.poll(0), mode.returncode(0)), (([], True), -signal.SIGHUP))

    def test_with_no_pane_bound_it_reads_no_screen_and_stops_killing_nothing(self):
        self.host.pane.return_value = None  # begin found no pane
        mode = self.prepare()
        mode.begin(("/bin/run",), cwd=self.tmp, env={})
        self.host.status.return_value = tmux_host.RUNNING
        self.assertEqual(mode.poll(0), ([], True))
        mode.stop()
        self.host.read.assert_not_called()
        self.host.kill.assert_not_called()
        self.assertNoSessionDir()

    def test_its_pane_unknown_is_no_scan_and_no_end(self):
        mode = self.started()
        self.host.pane.side_effect = tmux_host.TuiError("tmux: boom")
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], False))
        self.assertEqual(
            (self.run.screens, mode.returncode(0), mode.state()), ([], None, "unknown")
        )


class SwapTest(Base):
    """A new session takes the name right after the Mode's pane check: every later call targets its
    pane (%1), so none reaches the new pane (%2).
    """

    def swapped(self, screen: str = "") -> list:
        """After started(): the next pane(NAME) answers %1, then %2 holds the name and %1 is gone
        (absent to status and option, a TuiError to the rest). Returns (call, pane) for each call
        that reached a pane.
        """
        holder, reached = ["%1"], []

        def at(target):
            return holder[0] if target in (NAME, holder[0]) else None

        def check(name):
            found, holder[0] = holder[0], "%2"
            return found

        def act(method, result):
            def call(target, *args):
                pane = at(target)
                if pane is None and method != "option":
                    raise tmux_host.TuiError(f"tmux: can't find pane: {target}")
                if pane is not None:
                    reached.append((method, pane))
                return result

            return call

        self.host.pane.side_effect = check
        self.host.status.side_effect = lambda target: tmux_host.RUNNING if at(target) else None
        for method, result in (
            ("paste", None),
            ("keys", None),
            ("read", screen),
            ("option", None),
            ("kill", None),
        ):
            getattr(self.host, method).side_effect = act(method, result)
        return reached

    def error(self, call) -> str | None:
        try:
            call()
        except DriverError as e:
            return str(e)
        return None

    def test_send_never_types_into_the_new_pane(self):
        mode = self.started()
        reached = self.swapped()
        self.assertEqual(
            (self.error(lambda: mode.send("hi")), reached),
            ("send to tui session w1: tmux: can't find pane: %1", []),
        )

    def test_screen_never_reads_the_new_pane(self):
        mode = self.started()
        reached = self.swapped()
        self.assertEqual(
            (self.error(mode.screen), reached),
            ("screen of tui session w1: tmux: can't find pane: %1", []),
        )

    def test_poll_reads_and_marks_by_its_pane_only(self):
        mode = self.started()
        reached = self.swapped("Sign in")
        self.assertEqual((mode.poll(0), mode.poll(0)), (([Blocked("Sign in")], False), ([], True)))
        self.assertEqual(reached, [("read", "%1")])
        self.host.mark.assert_called_once_with("blocked", self.events, pane="%1")

    def test_stop_never_kills_the_new_session(self):
        mode = self.started()
        reached = self.swapped()
        mode.stop()
        self.assertEqual((reached, os.path.isdir(self.dir)), ([], False))
        self.host.kill.assert_called_once_with("%1")


class AttachTest(Base):
    def setUp(self):
        super().setUp()
        self.host.option.side_effect = lambda name, key: {"@sid": SID}.get(key)

    def attach(self, *lines) -> tui.TuiMode:
        """TuiMode.attach on a prepared session dir whose hooks.jsonl has lines, the host's calls
        cleared first.
        """
        self.prepare()
        self.append(*lines)
        self.host.reset_mock()
        return tui.TuiMode.attach(NAME, tui.read_state(NAME), self.run, host=self.host)

    def test_reply_bound_and_the_next_record_from_the_files_end(self):
        partial = (json.dumps(record("tool", tool="next")) + "\n").encode()
        mode = self.attach(
            record("tool", session_id="n1"),
            record("stop", session_id="n1", last_message="r1"),
            record("stop", session_id="n2", last_message="r2"),
            record("ask"),
            b"bad\n",
            partial[:7],
        )
        self.assertEqual(
            (mode.name, mode.sid, mode.events, mode.reply, mode.bound),
            (NAME, SID, self.events, "r2", "n2"),
        )
        self.assertEqual(
            self.host.mock_calls, [mock.call.pane(NAME), mock.call.option("%1", "@sid")]
        )
        self.append(partial[7:], record("stop", session_id="n2", last_message="r3"))
        self.assertEqual(mode.poll(0), ([Activity("next"), TurnEnd("r3", 0)], False))

    def test_no_records_yet(self):
        mode = self.attach()
        self.assertEqual((mode.reply, mode.bound), (None, ""))
        self.append(record("stop", session_id="n1"))
        self.assertEqual(mode.poll(0), ([SessionBound("n1"), TurnEnd("", 0)], False))

    def test_a_reply_of_the_last_turn_end_even_when_empty(self):
        mode = self.attach(record("stop", last_message="r1"), record("stop"), record("tool"))
        self.assertEqual(mode.reply, "")

    def test_blocked_by_its_last_record_of_a_kind_else_by_its_panes_state(self):
        self.host.pane.return_value = "%7"
        partial = json.dumps(record("tool")).encode()
        for lines, state, blocked in (
            ((record("tool"), record("ask"), record("other"), b"bad\n", partial), None, True),
            ((record("ask"), record("tool")), None, False),
            ((record("ask"), record("stop")), "done", False),
            ((record("stop"),), "blocked", True),
            ((), "blocked", True),
            ((), "working", False),
            ((), tmux_host.TuiError("tmux: denied"), False),
        ):
            with self.subTest(lines=lines, state=state):

                def option(target, key, *, state=state):
                    if (target, key) == ("%7", "@state") and isinstance(state, Exception):
                        raise state
                    return {("%7", "@sid"): SID, ("%7", "@state"): state}.get((target, key))

                self.host.option.side_effect = option
                self.assertIs(self.attach(*lines).blocked, blocked)

    def test_another_sid_absent_or_unknown_is_no_tui_session(self):
        for found in ("other", None, tmux_host.TuiError("tmux: denied")):
            with self.subTest(found=found):
                self.host.option.side_effect = (
                    found
                    if isinstance(found, Exception)
                    else lambda name, key, *, found=found: found
                )
                with self.assertRaises(DriverError) as caught:
                    self.attach()
                self.assertEqual(str(caught.exception), "no tui session w1")
        self.host.option.side_effect = lambda name, key: {"@sid": SID}.get(key)
        for pane in (None, tmux_host.TuiError("tmux: denied")):
            with self.subTest(pane=pane):
                self.host.pane.return_value = pane
                self.host.pane.side_effect = pane if isinstance(pane, Exception) else None
                with self.assertRaisesRegex(DriverError, r"\Ano tui session w1\Z"):
                    self.attach()
                self.host.option.assert_not_called()

    def test_it_reads_the_sid_of_the_pane_it_took_so_a_pane_swapped_in_is_never_bound(self):
        live = {"%7": SID}  # pane -> its session's @sid; the one pane holds the name

        def then_swap(answer):
            def call(*args):
                found = answer(*args)
                if self.host.pane.call_count + self.host.option.call_count == 1:
                    live.clear()
                    live["%8"] = SID  # a new session under the name
                return found

            return call

        self.host.pane.side_effect = then_swap(lambda name: next(iter(live)))
        self.host.option.side_effect = then_swap(
            lambda target, key: live.get(next(iter(live)) if target == NAME else target)
        )
        try:
            bound = self.attach()._pane
        except DriverError as e:
            bound = str(e)
        self.assertEqual(bound, "no tui session w1")
        self.assertEqual(
            self.host.mock_calls, [mock.call.pane(NAME), mock.call.option("%7", "@sid")]
        )

    def test_an_attached_session_is_live_and_marks_dialogs_on_its_events_file(self):
        self.host.pane.return_value = "%7"
        mode = self.attach()
        self.host.status.return_value = tmux_host.RUNNING
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([Blocked("Sign in")], False))
        self.host.mark.assert_called_once_with("blocked", self.events, pane="%7")
        mode.stop()
        self.host.kill.assert_called_once_with("%7")

    def test_an_attached_session_ends_once_another_pane_holds_its_name(self):
        self.host.pane.return_value = "%7"
        mode = self.attach()
        self.host.status.return_value, self.host.pane.return_value = tmux_host.RUNNING, "%8"
        self.host.read.return_value = "Sign in"
        self.assertEqual(mode.poll(0), ([], True))
        self.assertEqual((self.run.screens, mode.returncode(0)), ([], -signal.SIGHUP))


class ModeTest(Case):
    def test_tui_mode_has_each_mode_method_with_its_parameters(self):
        def params(function):
            return [
                (None if p.kind is inspect.Parameter.POSITIONAL_ONLY else p.name, p.kind, p.default)
                for p in inspect.signature(function).parameters.values()
            ]

        for name in ("poll", "returncode", "state", "send", "screen", "stop"):
            with self.subTest(name=name):
                self.assertEqual(params(getattr(tui.TuiMode, name)), params(getattr(Mode, name)))


if __name__ == "__main__":
    unittest.main()
