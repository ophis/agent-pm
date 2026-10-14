"""Process group ownership lasts until the controller reaps its own anchor."""

import json
import os
import signal
import subprocess
from unittest import mock

from agent_pm.utils.process_control import ProcessControl, _Group

from utils.tests.case import Case


class GroupTest(Case):
    def test_parent_does_not_race_an_exited_anchor_to_set_its_group(self):
        with (
            mock.patch("os.fork", return_value=4242),
            mock.patch("os.setpgid", side_effect=ProcessLookupError) as setpgid,
        ):
            group = _Group.launch(["missing"], "/")
        self.addCleanup(os.close, group.channel)
        setpgid.assert_not_called()
        self.assertEqual(group.receive(), [])
        self.assertTrue(group.eof)

    def group(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        os.write(write, b'{"started": true}\n{"rc": 0}\n')
        return _Group(4242, read)

    def test_unreaped_anchor_pins_descendants_then_disables_all_future_signals(self):
        group = self.group()
        with (
            mock.patch("os.killpg") as killpg,
            mock.patch("os.waitpid", return_value=(4242, 0)) as reap,
            mock.patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "4242 4242 Z\n4343 4242 S\n"),
            ) as ps,
        ):
            self.assertEqual(group.receive(), [{"started": True}, {"rc": 0}])
            self.assertTrue(group.live())
            reap.assert_not_called()
            group.kill()
            self.assertIn(mock.call(4242, signal.SIGKILL), killpg.call_args_list)
            ps.return_value.stdout = "4242 4242 Z\n"
            self.assertFalse(group.live())
            reap.assert_called_once_with(4242, 0)
            killpg.reset_mock()
            ps.return_value.stdout = "9999 4242 S\n"
            group.kill()
            self.assertFalse(group.live())
            killpg.assert_not_called()

    def test_permission_denied_retains_identity_and_never_reports_exit(self):
        group = self.group()
        with (
            mock.patch("subprocess.run", side_effect=PermissionError),
            mock.patch("os.killpg") as killpg,
            mock.patch("os.waitpid") as reap,
        ):
            for operation in (group.live, group.kill, group.live):
                with self.subTest(operation=operation), self.assertRaises(PermissionError):
                    operation()
                self.assertEqual(group.pid, 4242)
            reap.assert_not_called()
            killpg.assert_not_called()

    def test_permission_denied_signalling_does_not_forget_the_group(self):
        group = self.group()
        with (
            mock.patch("os.killpg", side_effect=[PermissionError, None]) as killpg,
            mock.patch(
                "subprocess.run", return_value=subprocess.CompletedProcess([], 0, "4343 4242 S\n")
            ),
        ):
            with self.assertRaises(PermissionError):
                group.kill()
            self.assertEqual(group.pid, 4242)
            group.kill()
            self.assertEqual(killpg.call_args_list[-1], mock.call(4242, signal.SIGKILL))


class ControlTest(Case):
    def test_a_completed_generation_never_contacts_or_signals_a_reused_group(self):
        control = ProcessControl.prepare(["program"], "/")
        self.addCleanup(control.remove)
        with open(os.path.join(control.path, "done.json"), "w") as stream:
            json.dump({"rc": 0}, stream)
        with mock.patch("socket.socket") as socket, mock.patch("os.killpg") as killpg:
            control.kill()
            self.assertFalse(control.live())
            socket.assert_not_called()
            killpg.assert_not_called()
