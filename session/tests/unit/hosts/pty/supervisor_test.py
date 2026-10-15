"""session/src/agent_pm/session/hosts/pty/supervisor.py: bracketed paste tracking and input
requests.
"""

import json
import unittest

from agent_pm.session.hosts.pty.supervisor import bracketing, request

ON, OFF = b"\x1b[?2004h", b"\x1b[?2004l"


class Bracketing(unittest.TestCase):
    def test_the_last_request_in_the_output_wins_else_it_stays(self):
        cases = [
            (b"text", False, False),
            (b"text", True, True),
            (b"a" + ON + b"b", False, True),
            (ON + OFF, False, False),
            (OFF + b"x" + ON, False, True),
            (OFF, True, False),
        ]
        for seen, was, now in cases:
            with self.subTest(seen=seen, was=was):
                self.assertEqual(bracketing(seen, was=was), now)


class Request(unittest.TestCase):
    def test_a_paste_is_its_utf_8_bracketed_when_asked(self):
        line = json.dumps({"paste": "é\nx"}).encode()
        self.assertEqual(request(line, bracketed=False), "é\nx".encode())
        self.assertEqual(
            request(line, bracketed=True), b"\x1b[200~" + "é\nx".encode() + b"\x1b[201~"
        )

    def test_keys_are_their_hex_never_bracketed(self):
        line = json.dumps({"keys": b"\x1b[A\x03".hex()}).encode()
        self.assertEqual(request(line, bracketed=True), b"\x1b[A\x03")

    def test_anything_else_is_a_value_error(self):
        for line in (b"", b"[]", b'{"paste": 1}', b'{"keys": "zz"}', b'{"paste": "a", "keys": ""}'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                request(line, bracketed=False)


if __name__ == "__main__":
    unittest.main()
