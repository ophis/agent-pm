"""Startup behaviour."""

import errno
import json
import os
import re
import stat
import sys
import threading
from unittest import mock

from agent_pm.session.adapters.types import ACTIVITY, BLOCKED, TURN_END
from agent_pm.session.engine.types import (
    DriverError,
    SessionInfo,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes.tui import types as tui

from session.tests.case import SRC
from session.tests.unit.tui.case import NAME, SID, Base, ScanTui


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
        info = SessionInfo(
            sid=SID,
            native_id="",
            cwd="/cwd",
            project=True,
            transcript="/t/x.jsonl",
            resume="fake --resume x",
        )
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
            (
                {
                    "info": SessionInfo(
                        sid=SID,
                        native_id="",
                        cwd=self.tmp,
                        project=False,
                        transcript="rel.jsonl",
                        resume="",
                    )
                },
                "transcript",
            ),
            (
                {
                    "info": SessionInfo(
                        sid=SID, native_id="", cwd="rel", project=False, transcript="", resume=""
                    )
                },
                "cwd",
            ),
            (
                {
                    "info": SessionInfo(
                        sid="a/b",
                        native_id="",
                        cwd=self.tmp,
                        project=False,
                        transcript="",
                        resume="",
                    )
                },
                "sid",
            ),
            ({"events": "rel"}, "events"),
            ({"workdir": "/a\nb"}, "workdir"),
            ({"run": BadLabel(self.run.adapter)}, "words"),
            ({"tmux": "bin/tmux"}, "tmux_path"),
            ({"tmux": None}, "tmux_path"),
        ):
            with (
                self.subTest(field),
                self.assertRaisesRegex(DriverError, f"\\Atui session w1: bad {field} "),
            ):
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
                with (
                    mock.patch("os.getuid", return_value=uid if uid is not None else os.getuid()),
                    self.assertRaisesRegex(DriverError, re.escape(self.root)),
                ):
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
