"""Begin behaviour."""

import json
import os
import stat
import threading
from unittest import mock

from agent_pm.session.engine.types import (
    DriverError,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes.tui import types as tui

from session.tests.unit.tui.case import NAME, SID, Base


class BeginTest(Base):
    def test_starts_the_session_tagged_with_its_sid_and_runtime(self):
        mode = self.prepare()
        mode.begin(["/bin/run", "a"], cwd=self.tmp, env={"A": "1"}, status_line=True)
        self.assertEqual(
            self.host.mock_calls[1:],
            [
                mock.call.start(
                    NAME,
                    tmux_host.Command(argv=["/bin/run", "a"], cwd=self.tmp, env={"A": "1"}),
                    options=tmux_host.StartOptions(
                        events=self.events,
                        status_line=True,
                        tags={"@sid": SID, "@runtime": "fake"},
                    ),
                ),
                mock.call.pane(NAME),
            ],
        )
        self.prepare(events=None).begin(("/bin/run",), cwd=self.tmp, env={})
        self.assertEqual(
            self.host.start.call_args,
            mock.call(
                NAME,
                tmux_host.Command(argv=["/bin/run"], cwd=self.tmp, env={}),
                options=tmux_host.StartOptions(
                    events=None,
                    status_line=False,
                    tags={"@sid": SID, "@runtime": "fake"},
                ),
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
