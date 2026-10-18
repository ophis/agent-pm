"""The Ollama listing probe over a fake HTTP connection."""

import http.client
import json
from unittest import mock

from agent_pm.session.clients.opencode import probe

from session.tests.case import Case


class Response:
    def __init__(self, status: int, body: bytes):
        self.status, self.body = status, body

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def connection(status=200, body=b'{"models": []}', error=None):
    found = mock.Mock()
    found.request.side_effect = error
    found.getresponse.return_value = Response(status, body)
    return found


class ProbeTest(Case):
    def listing(self, port="11434", **response):
        made = connection(**response)
        with mock.patch.object(probe.http.client, "HTTPConnection", return_value=made) as kind:
            found = probe.listing(port)
        return found, kind, made

    def test_a_listing_is_its_json_object(self):
        found, kind, made = self.listing()
        self.assertEqual(found, {"models": []})
        kind.assert_called_once_with("127.0.0.1", 11434, timeout=probe.TIMEOUT)
        made.request.assert_called_once_with("GET", "/api/tags")
        made.close.assert_called_once_with()

    def test_anything_else_is_none(self):
        for response in (
            {"status": 404},
            {"error": OSError("refused")},
            {"error": http.client.HTTPException("bad")},
            {"body": b"not json"},
            {"body": b"[]"},
            {"body": json.dumps({"m": "x" * probe.LIMIT}).encode()},
        ):
            with self.subTest(response=str(response)[:60]):
                self.assertIsNone(self.listing(**response)[0])

    def test_a_bad_port_asks_nothing(self):
        for port in ("0", "65536", "x", "-1"):
            with self.subTest(port=port):
                found, kind, _ = self.listing(port)
                self.assertIsNone(found)
                kind.assert_not_called()
