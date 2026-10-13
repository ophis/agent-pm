"""The Codex app-server client's answer matching, line bounds and process cleanup."""

from unittest import mock

from agent_pm.session.adapters.codex import app_server

from session.tests.case import Case


class ClientTest(Case):
    def test_answer_ignores_notifications_wrong_ids_and_malformed_json(self):
        lines = iter(
            [
                b"not json",
                b'{"id":true,"result":1}',
                b'{"method":"event","params":{}}',
                b'{"id":1,"result":{}}',
                b'{"id":2,"result":{"data":[]}}',
            ]
        )
        self.assertEqual(app_server.answer(lines, 2, "hooks/list"), {"data": []})
        with self.assertRaisesRegex(app_server.Error, "denied"):
            app_server.answer(iter([b'{"id":2,"error":{"message":"denied"}}']), 2, "hooks/list")

    def test_line_bounds_and_timeout(self):
        with mock.patch("time.monotonic", return_value=1):
            with self.assertRaisesRegex(app_server.Error, "no answer"):
                next(app_server.Lines(42, 0, 1))
        with (
            mock.patch("select.select", return_value=([42], [], [])),
            mock.patch("os.read", return_value=b"x"),
        ):
            lines = app_server.Lines(42, 999999999999, 8)
            lines.buf = bytearray(8)
            with self.assertRaisesRegex(app_server.Error, "over"):
                next(lines)

    def test_process_cleanup_kills_timed_out_group(self):
        proc = mock.Mock(pid=123)
        proc.stdout.fileno.return_value = 42
        proc.wait.side_effect = [app_server.subprocess.TimeoutExpired("fake", 1), None]
        with (
            mock.patch("select.select", return_value=([], [], [])),
            mock.patch("os.killpg") as kill,
            mock.patch("time.monotonic", side_effect=[0, 0, 0, 6]),
        ):
            app_server.close(proc)
        kill.assert_called_once_with(123, app_server.signal.SIGKILL)
        proc.stdout.close.assert_called_once()

    def test_normal_probe_exit_kills_remaining_owned_group(self):
        proc = mock.Mock(pid=123)
        proc.stdout.fileno.return_value = 42
        proc.wait.return_value = 0
        with (
            mock.patch("select.select", return_value=([], [], [])),
            mock.patch("os.killpg") as kill,
        ):
            app_server.close(proc)
        kill.assert_called_once_with(123, app_server.signal.SIGKILL)
        proc.wait.assert_called()
