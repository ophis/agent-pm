"""utils/markdown.py: fenced blocks; unfenced is composer's (base_test.py tests it)."""
import unittest

from new.tests import SRC  # noqa: F401
from composer import base
from utils.markdown import fenced, unfenced


class Fenced(unittest.TestCase):
    def test_backtick_and_tilde_blocks_in_order_with_their_info_strings_stripped(self):
        text = "a\n```  agent-pm-outcome  \n{}\n```\nb\n~~~ js\nx\ny\n~~~\n```\n```\n"
        self.assertEqual(fenced(text), [("agent-pm-outcome", "{}"), ("js", "x\ny"), ("", "")])

    def test_a_longer_closing_fence_closes(self):
        self.assertEqual(fenced("```x\nbody\n`````\nafter\n```y\nz\n  ```  \n"), [("x", "body"), ("y", "z")])

    def test_a_block_never_closed_runs_to_the_end(self):
        self.assertEqual(fenced("a\n````md\nx\n```\ny"), [("md", "x\n```\ny")])
        self.assertEqual(fenced("```\n"), [("", "")])

    def test_a_fence_line_inside_a_longer_or_other_block_is_body(self):
        text = "````outer\n```inner\nx\n```\n~~~\n````\n~~~~\n```\n~~~\n~~~~\n"
        self.assertEqual(fenced(text), [("outer", "```inner\nx\n```\n~~~"), ("", "```\n~~~")])

    def test_a_closing_line_holds_only_the_fence_char(self):
        self.assertEqual(fenced("```\nx\n``` y\n```~\n```\n"), [("", "x\n``` y\n```~")])

    def test_no_fence_is_no_block(self):
        for text in ("", "plain\ntext", "``\nx\n``", "a ```b```"):
            with self.subTest(text=text):
                self.assertEqual(fenced(text), [])


class Moved(unittest.TestCase):
    def test_composer_uses_these(self):
        self.assertIs(base.unfenced, unfenced)


if __name__ == "__main__":
    unittest.main()
