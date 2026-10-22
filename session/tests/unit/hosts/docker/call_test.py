"""The fixed host command transport, using only fake streams, sockets and child processes."""

import errno
import io
import json
import os
import stat
from types import SimpleNamespace
from unittest import mock

from agent_pm.session.hosts.docker import call

from session.tests.case import Case
from utils.tests.process_fixtures import DEVNULL, PIPE, TimeoutExpired

GEN = "0123456789abcdef0123456789abcdef"
CONFIG = {
    "generation": GEN,
    "argv": ["/host/bin/check", "--fixed"],
    "env": {"AGENT_PM_CLIENT": "client"},
}


def wire(value):
    return json.dumps(value).encode() + b"\n"


def ready():
    return {"ready": True, "generation": GEN}


class CommandTest(Case):
    def test_host_executes_only_preconfigured_argv_env_and_root_cwd(self):
        def run(argv, **kwargs):
            self.assertEqual(argv, CONFIG["argv"])
            self.assertEqual(kwargs["cwd"], "/")
            self.assertEqual(kwargs["timeout"], 150)
            self.assertEqual(kwargs["stderr"], DEVNULL)
            self.assertEqual(kwargs["env"]["KEEP"], "host")
            self.assertEqual(kwargs["env"]["AGENT_PM_CLIENT"], "client")
            kwargs["stdout"].write(b"usage is clear\n")
            return SimpleNamespace(returncode=0)

        with mock.patch.dict(os.environ, {"KEEP": "host", "AGENT_PM_CLIENT": "old"}):
            self.assertEqual(
                call.execute(CONFIG, run=run),
                {"returncode": 0, "stdout": "usage is clear\n"},
            )

    def test_brake_and_signal_status_and_stdout_are_preserved(self):
        for status, expected in ((1, 1), (80, 80), (-15, 143)):
            with self.subTest(status=status):

                def run(_argv, status=status, **kwargs):
                    kwargs["stdout"].write(b"stop\n")
                    return SimpleNamespace(returncode=status)

                self.assertEqual(
                    call.execute(CONFIG, run=run), {"returncode": expected, "stdout": "stop\n"}
                )

    def test_command_failures_are_fixed_and_never_include_exception_text(self):
        for error in (FileNotFoundError("host-secret"), TimeoutExpired("secret", 150)):
            with self.subTest(error=type(error).__name__):
                result = call.execute(CONFIG, run=mock.Mock(side_effect=error))
                self.assertEqual(result["returncode"], 2)
                self.assertEqual(result["stdout"], call.FAILURE)

    def test_output_is_bounded_and_invalid_utf8_fails_closed(self):
        for output in (b"x" * (call.OUTPUT_MAX + 1), b"\xff"):
            with self.subTest(length=len(output)):

                def run(_argv, output=output, **kwargs):
                    kwargs["stdout"].write(output)
                    return SimpleNamespace(returncode=0)

                self.assertEqual(call.execute(CONFIG, run=run), call.failed())


class ProtocolTest(Case):
    def test_request_is_exactly_an_empty_object_and_host_request_has_only_generation(self):
        self.assertEqual(call.request(b"{}\n"), {})
        for bad in (b"[]\n", b"{}", b"{}\n{}\n", b'{"argv":[]}\n', b"x" * 257 + b"\n"):
            with self.subTest(bad=bad[:50]):
                self.assertIsNone(call.request(bad))
        self.assertTrue(call.host_request(wire({"generation": GEN}), GEN))
        for value in ({"generation": "f" * 32}, {"generation": GEN, "argv": []}, {}):
            self.assertFalse(call.host_request(wire(value), GEN))
        self.assertFalse(call.host_request(b'{"generation":"x","generation":"x"}\n', GEN))

    def test_reply_validation_cannot_turn_malformed_output_into_success(self):
        self.assertEqual(
            call.reply(wire({"returncode": 0, "stdout": "ok"})), {"returncode": 0, "stdout": "ok"}
        )
        for value in (
            {"returncode": True, "stdout": ""},
            {"returncode": 256, "stdout": ""},
            {"returncode": -1, "stdout": ""},
            {"returncode": 0, "stdout": []},
            {"returncode": 0, "stdout": "", "env": {}},
            {"returncode": 0, "stdout": "x" * (call.OUTPUT_MAX + 1)},
        ):
            with self.subTest(value=str(value)[:90]):
                self.assertIsNone(call.reply(wire(value)))
        for line in (
            b'{"returncode":0,"returncode":1,"stdout":""}\n',
            b'{"returncode":NaN,"stdout":""}\n',
            b"\xff\n",
        ):
            self.assertIsNone(call.reply(line))

    def test_config_requires_absolute_command_and_only_fixed_environment_keys(self):
        self.assertEqual(call.configuration(CONFIG), CONFIG)
        for changes in (
            {"argv": ["relative"]},
            {"argv": []},
            {"argv": ["/bin/check", "bad\x00arg"]},
            {"generation": "not-a-generation"},
            {"env": {"AGENT_PM_CLIENT": "client", "EXTRA": "secret"}},
            {"extra": "data"},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(call.configuration({**CONFIG, **changes}))


class TransportTest(Case):
    def test_client_sends_empty_request_and_forwards_brake_result(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.return_value = wire({"returncode": 80, "stdout": "brake\n"})
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(call.socket, "socket", return_value=connection):
            self.assertEqual(call.client(stdout=output, stderr=errors), 80)
        connection.connect.assert_called_once_with(call.SOCKET)
        connection.sendall.assert_called_once_with(b"{}\n")
        self.assertEqual(output.getvalue(), "brake\n")
        self.assertEqual(errors.getvalue(), "")

    def test_client_missing_socket_or_malformed_reply_fails_with_fixed_stderr(self):
        for error, response in ((OSError("secret"), b""), (None, b"not json\n"), (None, b"")):
            with self.subTest(error=error, response=response):
                connection = mock.MagicMock()
                connection.__enter__.return_value = connection
                connection.connect.side_effect = error
                connection.recv.return_value = response
                output, errors = io.StringIO(), io.StringIO()
                with mock.patch.object(call.socket, "socket", return_value=connection):
                    self.assertEqual(call.client(stdout=output, stderr=errors), 2)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(errors.getvalue(), call.FAILURE)

    def test_socket_read_stops_at_eof_and_bounds_partial_or_oversized_messages(self):
        connection = mock.Mock()
        connection.recv.side_effect = [b'{"return', b'code":0,"stdout":"ok"}\n']
        self.assertEqual(
            call.read_socket(connection), wire({"returncode": 0, "stdout": "ok"}).replace(b" ", b"")
        )
        connection.recv.side_effect = [b"{}", b""]
        self.assertEqual(call.read_socket(connection), b"{}")
        connection.recv.side_effect = [b"x" * (call.LINE_MAX + 1)]
        with self.assertRaises(ValueError):
            call.read_socket(connection)

    def test_serve_handshake_and_only_valid_requests_reach_host(self):
        listener, first, second = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        listener.__enter__.return_value = listener
        first.__enter__.return_value, second.__enter__.return_value = first, second
        first.recv.return_value, second.recv.return_value = b'{"argv":[]}\n', b"{}\n"
        listener.accept.side_effect = [(first, None), (second, None), OSError("done")]
        source = io.BytesIO(wire({"returncode": 1, "stdout": "stop"}))
        destination = io.BytesIO()
        with mock.patch.object(
            call.select, "select", side_effect=lambda readers, *_: (readers[:1], [], [])
        ):
            self.assertEqual(
                call.serve(
                    GEN, stdin=source, stdout=destination, listener_factory=lambda _: listener
                ),
                2,
            )
        self.assertEqual(
            [json.loads(x) for x in destination.getvalue().splitlines()],
            [ready(), {"generation": GEN}],
        )
        self.assertEqual(json.loads(first.sendall.call_args.args[0]), call.failed())
        self.assertEqual(
            json.loads(second.sendall.call_args.args[0]), {"returncode": 1, "stdout": "stop"}
        )

    def test_host_handshake_generation_validation_and_serial_result_forwarding(self):
        process = mock.Mock()
        process.stdout = io.BytesIO(
            wire(ready()) + wire({"generation": "f" * 32}) + wire({"generation": GEN})
        )
        process.stdin = io.BytesIO()
        process.poll.return_value = None
        destination = io.BytesIO()

        def run(_argv, **kwargs):
            kwargs["stdout"].write(b"stop")
            return SimpleNamespace(returncode=80)

        invoke = mock.Mock(side_effect=run)
        popen = mock.Mock(return_value=process)
        config_path = os.path.join(self.home, "command.json")
        with open(config_path, "w") as stream:
            json.dump(CONFIG, stream)
        with (
            mock.patch.object(
                call.select, "select", side_effect=lambda readers, *_: (readers, [], [])
            ),
            mock.patch("subprocess.run", invoke),
        ):
            self.assertEqual(
                call.host(
                    config_path, ["/bin/connector", "serve", GEN], stdout=destination, popen=popen
                ),
                0,
            )
        popen.assert_called_once_with(
            ["/bin/connector", "serve", GEN],
            stdin=PIPE,
            stdout=PIPE,
            stderr=DEVNULL,
        )
        self.assertEqual(json.loads(destination.getvalue()), ready())
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(invoke.call_args.args[0], CONFIG["argv"])
        self.assertEqual(
            [json.loads(x) for x in process.stdin.getvalue().splitlines()],
            [call.failed(), {"returncode": 80, "stdout": "stop"}],
        )
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        self.assertFalse(os.path.exists(config_path + ".ready"))

    def test_host_handshake_timeout_or_wrong_generation_never_announces_ready(self):
        for data, selected in ((wire({"ready": True, "generation": "f" * 32}), True), (b"", False)):
            with self.subTest(data=data):
                process = mock.Mock(stdout=io.BytesIO(data), stdin=io.BytesIO())
                process.poll.return_value = 0
                destination = io.BytesIO()
                config_path = os.path.join(self.home, "command.json")
                with open(config_path, "w") as stream:
                    json.dump(CONFIG, stream)
                with mock.patch.object(
                    call.select,
                    "select",
                    side_effect=lambda readers, *_, selected=selected: (
                        readers if selected else [],
                        [],
                        [],
                    ),
                ):
                    self.assertEqual(
                        call.host(
                            config_path,
                            ["/bin/connector"],
                            stdout=destination,
                            popen=mock.Mock(return_value=process),
                        ),
                        2,
                    )
                self.assertEqual(destination.getvalue(), b"")

    def test_duplicate_host_holds_no_second_connector(self):
        path = os.path.join(self.home, "command.json")
        with open(path, "w") as stream:
            json.dump(CONFIG, stream)
        with open(path + ".ready", "wb") as stream:
            stream.write(wire(ready()))
        destination, popen = io.BytesIO(), mock.Mock()
        with mock.patch.object(
            call.fcntl, "flock", side_effect=BlockingIOError(errno.EAGAIN, "busy")
        ):
            self.assertEqual(
                call.host(path, ["/bin/connector"], stdout=destination, popen=popen), 0
            )
        self.assertEqual(json.loads(destination.getvalue()), ready())
        popen.assert_not_called()

    def test_idle_container_exits_on_host_eof_without_accepting_a_client(self):
        server = mock.MagicMock()
        server.__enter__.return_value = server
        source, destination = io.BytesIO(), io.BytesIO()
        with mock.patch.object(call.select, "select", return_value=([source], [], [])):
            self.assertEqual(
                call.serve(
                    GEN, stdin=source, stdout=destination, listener_factory=lambda _: server
                ),
                2,
            )
        server.accept.assert_not_called()
        self.assertEqual(json.loads(destination.getvalue()), ready())

    def test_client_disconnect_does_not_block_the_next_request(self):
        server, connection = mock.MagicMock(), mock.MagicMock()
        server.__enter__.return_value = server
        connection.__enter__.return_value = connection
        connection.recv.return_value = b"{}\n"
        connection.sendall.side_effect = BrokenPipeError("gone")
        server.accept.return_value = (connection, None)
        source = io.BytesIO(wire({"returncode": 0, "stdout": "ok"}))
        destination = io.BytesIO()
        with mock.patch.object(
            call.select,
            "select",
            side_effect=[([server], [], []), ([source], [], []), ([source], [], [])],
        ):
            self.assertEqual(
                call.serve(
                    GEN, stdin=source, stdout=destination, listener_factory=lambda _: server
                ),
                2,
            )
        connection.sendall.assert_called_once()
        self.assertEqual(len(destination.getvalue().splitlines()), 2)

    def test_oversized_request_does_not_reach_host_and_malformed_host_reply_fails(self):
        for incoming, response in (
            (b"x" * (call.REQUEST_MAX + 1), b""),
            (b"{}\n", b"invalid\n"),
            (b"{}\n", b""),
        ):
            with self.subTest(incoming=incoming, response=response):
                server, connection = mock.MagicMock(), mock.MagicMock()
                server.__enter__.return_value = server
                connection.__enter__.return_value = connection
                connection.recv.return_value = incoming
                server.accept.return_value = (connection, None)
                source, destination = io.BytesIO(response), io.BytesIO()
                with mock.patch.object(
                    call.select, "select", side_effect=[([server], [], []), ([source], [], [])]
                ):
                    self.assertEqual(
                        call.serve(
                            GEN,
                            stdin=source,
                            stdout=destination,
                            listener_factory=lambda _, server=server: server,
                        ),
                        2,
                    )
                self.assertEqual(json.loads(connection.sendall.call_args.args[0]), call.failed())
                self.assertEqual(
                    len(destination.getvalue().splitlines()),
                    1 if len(incoming) > call.REQUEST_MAX else 2,
                )

    def test_unready_duplicate_never_announces_success_and_does_not_start_connector(self):
        path = os.path.join(self.home, "command.json")
        with open(path, "w") as stream:
            json.dump(CONFIG, stream)
        destination, popen = io.BytesIO(), mock.Mock()
        with (
            mock.patch.object(call.fcntl, "flock", side_effect=BlockingIOError),
            mock.patch.object(call.time, "monotonic", side_effect=[0, 11]),
        ):
            self.assertEqual(
                call.host(path, ["/bin/connector"], stdout=destination, popen=popen), 2
            )
        self.assertEqual(destination.getvalue(), b"")
        popen.assert_not_called()

    def test_duplicate_readiness_wait_is_bounded_and_generation_specific(self):
        path = os.path.join(self.home, "ready")
        with open(path, "wb") as stream:
            stream.write(wire({"ready": True, "generation": "f" * 32}))
        for marker in (path, path + ".missing"):
            with (
                mock.patch.object(call.time, "monotonic", side_effect=[0, 1, 11]),
                mock.patch.object(call.time, "sleep") as sleep,
            ):
                self.assertFalse(call._await_ready(marker, ready()))
            sleep.assert_called_once_with(0.05)

    def test_host_rejects_bad_config_connector_and_missing_connector_without_leaking(self):
        path = os.path.join(self.home, "command.json")
        for config in ({}, {**CONFIG, "env": {}}, CONFIG):
            with open(path, "w") as stream:
                json.dump(config, stream)
            output, spawn = io.BytesIO(), mock.Mock(side_effect=FileNotFoundError("secret"))
            self.assertEqual(call.host(path, ["/missing"], stdout=output, popen=spawn), 2)
            self.assertEqual(output.getvalue(), b"")
        for connector in ([], "not a list", [1], ["bad\0arg"]):
            self.assertEqual(call.host(path, connector, stdout=io.BytesIO()), 2)

    def test_stop_kills_and_reaps_a_connector_that_ignores_termination(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = [TimeoutExpired("fixed", 5), 0]
        call._stop(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        process.poll.side_effect = OSError("secret")
        call._stop(process)

    def test_stream_reader_preserves_queued_lines_and_rejects_overflow(self):
        source = io.BytesIO(b"one\ntwo\nlast")
        reader = call._Reader(source)
        self.assertEqual(reader.line(8), b"one\n")
        self.assertEqual(reader.line(8), b"two\n")
        self.assertEqual(reader.line(8), b"last")
        self.assertEqual(reader.line(8), b"")
        for content in (b"x" * 10, b"x" * 9 + b"\n"):
            with self.assertRaises(ValueError):
                call._Reader(io.BytesIO(content)).line(8)

    def test_main_dispatch_and_all_invalid_modes_fail_with_fixed_stderr(self):
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.recv.return_value = wire({"returncode": 80, "stdout": ""})
        with mock.patch.object(call.socket, "socket", return_value=connection):
            self.assertEqual(call.main(["call"]), 80)
        for argv in (
            [],
            ["unknown"],
            ["host", "/config", "malformed"],
            ["serve", "bad-generation"],
        ):
            errors = io.StringIO()
            with mock.patch.object(call.sys, "stderr", errors):
                self.assertEqual(call.main(argv), 2)
            self.assertEqual(errors.getvalue(), call.FAILURE)


class ListenerTest(Case):
    def test_stale_socket_is_probed_before_replacement_and_own_inode_is_removed(self):
        path = os.path.join(self.home, "call.sock")
        server, probe = mock.MagicMock(), mock.MagicMock()
        server.__enter__.return_value, probe.__enter__.return_value = server, probe
        server.bind.side_effect = [OSError(errno.EADDRINUSE, "busy"), None]
        probe.connect.side_effect = OSError(errno.ECONNREFUSED, "stale")
        old = os.stat_result((stat.S_IFSOCK | 0o600, 10, 1, 1, 0, 0, 0, 0, 0, 0))
        own = os.stat_result((stat.S_IFSOCK | 0o600, 11, 1, 1, 0, 0, 0, 0, 0, 0))
        original_lstat, entries = os.lstat, iter([old, old, own, own])

        def lstat(filename, *args, **kwargs):
            return next(entries) if filename == path else original_lstat(filename, *args, **kwargs)

        with (
            mock.patch.object(call.socket, "socket", side_effect=[server, probe]),
            mock.patch.object(os, "lstat", side_effect=lstat),
            mock.patch.object(os, "unlink") as unlink,
            mock.patch.object(os, "chmod") as chmod,
            call.listener(path) as found,
        ):
            self.assertIs(found, server)
        probe.connect.assert_called_once_with(path)
        self.assertEqual(unlink.call_args_list, [mock.call(path), mock.call(path)])
        self.assertEqual(
            chmod.call_args_list, [mock.call(self.home, 0o700), mock.call(path, 0o600)]
        )

    def test_live_socket_is_never_unlinked(self):
        path = os.path.join(self.home, "call.sock")
        server, probe = mock.MagicMock(), mock.MagicMock()
        server.__enter__.return_value, probe.__enter__.return_value = server, probe
        server.bind.side_effect = OSError(errno.EADDRINUSE, "busy")
        existing = os.stat_result((stat.S_IFSOCK | 0o600, 10, 1, 1, 0, 0, 0, 0, 0, 0))
        original_lstat = os.lstat

        def lstat(filename, *args, **kwargs):
            return existing if filename == path else original_lstat(filename, *args, **kwargs)

        with (
            mock.patch.object(call.socket, "socket", side_effect=[server, probe]),
            mock.patch.object(os, "lstat", side_effect=lstat),
            mock.patch.object(os, "unlink") as unlink,
            mock.patch.object(os, "chmod"),
            self.assertRaises(OSError),
            call.listener(path),
        ):
            self.fail("a live listener was replaced")
        unlink.assert_not_called()
