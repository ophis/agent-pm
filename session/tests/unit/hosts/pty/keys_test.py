"""session/src/agent_pm/session/hosts/pty/keys.py: send --key's notation as xterm's bytes."""

import unittest

from agent_pm.session import ConfigError
from agent_pm.session.engine.types import KeyPress, NamedKey
from agent_pm.session.hosts.pty.keys import encode

from session.tests.case import Case

NAMED = {
    "Enter": b"\r",
    "Escape": b"\x1b",
    "Tab": b"\t",
    "BTab": b"\x1b[Z",
    "BSpace": b"\x7f",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Right": b"\x1b[C",
    "Left": b"\x1b[D",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "PageUp": b"\x1b[5~",
    "PageDown": b"\x1b[6~",
    "F1": b"\x1bOP",
    "F2": b"\x1bOQ",
    "F3": b"\x1bOR",
    "F4": b"\x1bOS",
    "F5": b"\x1b[15~",
    "F6": b"\x1b[17~",
    "F7": b"\x1b[18~",
    "F8": b"\x1b[19~",
    "F9": b"\x1b[20~",
    "F10": b"\x1b[21~",
    "F11": b"\x1b[23~",
    "F12": b"\x1b[24~",
}


def sent(text: str) -> bytes:
    return encode(KeyPress.parse(text))


class Encode(Case):
    def test_every_named_key_is_its_xterm_sequence(self):
        self.assertEqual(set(NAMED), set(NamedKey))
        for name, data in NAMED.items():
            with self.subTest(name=name):
                self.assertEqual(sent(name), data)

    def test_a_printable_character_is_its_utf_8(self):
        for char in ("q", "3", " ", ";", "-", "é"):
            with self.subTest(char=char):
                self.assertEqual(sent(char), char.encode())

    def test_ctrl_is_the_control_byte_of_a_letter_or_of_xterms_punctuation(self):
        cases = {"C-c": b"\x03", "C-A": b"\x01", "C-z": b"\x1a", "C-@": b"\x00", "C- ": b"\x00"}
        cases |= {"C-[": b"\x1b", "C-\\": b"\x1c", "C-]": b"\x1d", "C-^": b"\x1e"}
        cases |= {"C-_": b"\x1f", "C-/": b"\x1f", "C-?": b"\x7f"}
        for text, data in cases.items():
            with self.subTest(text=text):
                self.assertEqual(sent(text), data)

    def test_meta_puts_escape_before_a_character_or_a_plain_key(self):
        cases = {
            "M-x": b"\x1bx",
            "M-C-c": b"\x1b\x03",
            "M-Enter": b"\x1b\r",
            "M-BTab": b"\x1b\x1b[Z",
        }
        for text, data in cases.items():
            with self.subTest(text=text):
                self.assertEqual(sent(text), data)

    def test_shift_tab_is_btab(self):
        self.assertEqual(sent("S-Tab"), b"\x1b[Z")

    def test_a_modified_cursor_page_or_function_key_carries_xterms_modifier_parameter(self):
        cases = {
            "S-Up": b"\x1b[1;2A",
            "M-Left": b"\x1b[1;3D",
            "C-Right": b"\x1b[1;5C",
            "C-S-Home": b"\x1b[1;6H",
            "C-M-S-End": b"\x1b[1;8F",
            "S-F1": b"\x1b[1;2P",
            "C-F4": b"\x1b[1;5S",
            "S-PageUp": b"\x1b[5;2~",
            "M-F5": b"\x1b[15;3~",
            "C-F12": b"\x1b[24;5~",
        }
        for text, data in cases.items():
            with self.subTest(text=text):
                self.assertEqual(sent(text), data)

    def test_a_key_xterm_cannot_send_with_its_modifiers_is_refused(self):
        for text in (
            "S-a",
            "S-1",
            "C-1",
            "C-é",
            "C-Enter",
            "S-Escape",
            "C-Tab",
            "S-BTab",
            "C-BSpace",
        ):
            with self.subTest(text=text), self.assertRaises(ConfigError) as caught:
                sent(text)
            self.assertEqual(str(caught.exception), f"key {text}: the pty host cannot send it")


if __name__ == "__main__":
    unittest.main()
