"""Control behaviour."""

import inspect
import os
import shutil
from unittest import mock

from agent_pm.session.engine.lifecycle.types import Mode
from agent_pm.session.engine.types import (
    DriverError,
)
from agent_pm.session.hosts.tmux import tmux_host
from agent_pm.session.modes.tui import types as tui

from session.tests.case import Case
from session.tests.unit.tui.case import NAME, Base, Keys, record


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
        self.host.kill.side_effect = lambda _name: held.append(self.locked())
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
