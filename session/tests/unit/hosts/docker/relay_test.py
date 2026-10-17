"""hosts/docker/relay.py and stub.py: request forwarding, ack matching and the stub's exits, over
fake connections and pipes.
"""

import base64
import io
import json
import os
from unittest import mock

from agent_pm.session.hosts.docker import relay, stub

from session.tests.case import Case


class Pipe:
    def __init__(self, case):
        self.read, self.write = os.pipe()
        self.open = {self.read, self.write}
        case.addCleanup(lambda: [self.close(fd) for fd in list(self.open)])

    def close(self, fd):
        self.open.discard(fd)
        os.close(fd)

    def send(self, *lines):
        for line in lines:
            os.write(self.write, line)


class Conn:
    """A connected client: `request` to read, what the relay sends kept in `sent`."""

    def __init__(self, request):
        self.request, self.sent, self.timeout = request, b"", None

    def settimeout(self, timeout):
        self.timeout = timeout

    def makefile(self, mode):
        return io.BytesIO(self.request)

    def sendall(self, data):
        self.sent += data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


class RelayTest(Case):
    def setUp(self):
        self.acks = Pipe(self)

    def serve(self, request, *acks, wait=1.0):
        conn = Conn(request)
        self.acks.send(*acks)
        out = io.BytesIO()
        relay.serve(conn, out, relay.Acks(self.acks.read), wait)
        self.assertEqual(conn.timeout, wait)
        return out.getvalue(), conn.sent

    def test_forwards_the_request_and_returns_its_ack_dropping_stale_ones(self):
        request = b'{"id": 7, "argv": [], "payload": ""}\n'
        forwarded, back = self.serve(
            request, b'{"id": 6, "ok": true}\n', b"junk\n", b'{"id": 7, "ok": true}\n'
        )
        self.assertEqual((forwarded, back), (request, b'{"id": 7, "ok": true}\n'))

    def test_no_matching_ack_in_time_sends_nothing(self):
        forwarded, back = self.serve(b'{"id": 7}\n', b'{"id": 6, "ok": true}\n', wait=0.2)
        self.assertEqual((forwarded, back), (b'{"id": 7}\n', b""))

    def test_a_request_without_its_newline_is_not_forwarded(self):
        self.assertEqual(self.serve(b'{"id": 7}', b'{"id": 7, "ok": true}\n'), (b"", b""))

    def test_ids_and_acks_lines(self):
        for line, want in ((b'{"id": 3}', 3), (b'{"id": true}', None), (b"[]", None), (b"{", None)):
            with self.subTest(line=line):
                self.assertEqual(relay.request_id(line), want)
        acks = relay.Acks(self.acks.read)
        self.acks.send(b"a\nb")
        self.assertEqual(acks.line(0.2), b"a\n")
        self.assertIsNone(acks.line(0.1))
        self.acks.close(self.acks.write)
        self.assertIsNone(acks.line(1))
        self.assertTrue(acks.ended)

    def test_main_serves_until_stdin_ends_and_removes_its_socket(self):
        path = os.path.join(self.home, "relay.sock")
        listener = mock.Mock()
        conn = Conn(b'{"id": 1}\n')
        conns = [conn]

        def accept():
            self.acks.send(b'{"id": 1, "ok": true}\n')
            self.acks.close(self.acks.write)
            return conns.pop(), None

        listener.accept.side_effect = accept
        ready = iter([[listener], [self.acks.read]])
        out = io.BytesIO()
        with (
            mock.patch("socket.socket", return_value=listener),
            mock.patch("select.select", side_effect=lambda r, *_: (next(ready, r[-1:]), [], [])),
            mock.patch("sys.stdin", mock.Mock(fileno=lambda: self.acks.read)),
            mock.patch("sys.stdout", mock.Mock(buffer=out)),
        ):
            open(path, "w").close()
            self.assertEqual(relay.main(path), 0)
        listener.bind.assert_called_once_with(path)
        self.assertEqual(out.getvalue(), b'{"id": 1}\n')
        self.assertEqual(conn.sent, b'{"id": 1, "ok": true}\n')
        self.assertFalse(os.path.exists(path))


class StubTest(Case):
    def conn(self, *, ack=b'{"id": 1, "ok": true}\n', error=None):
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.connect.side_effect = error
        conn.makefile.return_value.__enter__.return_value = io.BytesIO(ack)
        return conn

    def run_stub(self, conn):
        err = io.StringIO()
        with mock.patch("socket.socket", return_value=conn), mock.patch("sys.stderr", err):
            code = stub.main(["hook", "/tui/w", "Stop"], io.BytesIO(b'{"a": 1}'))
        return code, err.getvalue()

    def test_sends_argv_and_payload_and_exits_0_on_its_ack(self):
        conn = self.conn()
        self.assertEqual(self.run_stub(conn), (0, ""))
        conn.connect.assert_called_once_with(stub.SOCKET)
        conn.settimeout.assert_called_once_with(stub.WAIT)
        sent = json.loads(conn.sendall.call_args.args[0])
        self.assertEqual(set(sent), {"id", "argv", "payload"})
        self.assertEqual(sent["argv"], ["hook", "/tui/w", "Stop"])
        self.assertEqual(base64.b64decode(sent["payload"]), b'{"a": 1}')
        self.assertIsInstance(sent["id"], int)

    def test_no_relay_or_no_ack_still_exits_0_with_one_line(self):
        for conn, want in (
            (self.conn(error=FileNotFoundError(2, "No such file or directory")), "No such file"),
            (self.conn(error=ConnectionRefusedError(61, "Connection refused")), "refused"),
            (self.conn(error=TimeoutError("timed out")), "timed out"),
            (self.conn(ack=b""), "no ack"),
        ):
            with self.subTest(want=want):
                code, err = self.run_stub(conn)
                self.assertEqual(code, 0)
                self.assertIn(want, err)
                self.assertEqual(len(err.splitlines()), 1)
