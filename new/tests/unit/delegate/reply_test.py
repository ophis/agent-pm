"""delegate/reply.py: progress mark lines and the outcome block of a reply (FR-18)."""

from new.tests.case import Case

import unittest

from agent_pm.composer import PROGRESS
from agent_pm.delegate.reply import MARK, marks, outcome_block


class Marks(Case):
    def test_a_mark_line_amid_body_text_is_one_mark(self):
        text = "Reading the task.\n[agent-pm-progress:start] first build of the PRD\nNow the spec."
        self.assertEqual(marks(text), [("start", "first build of the PRD")])

    def test_a_mark_and_an_outcome_block_are_one_mark_and_the_block(self):
        text = '[agent-pm-progress:start] the build\nDone.\n```agent-pm-outcome\n{"status": "done"}\n```\n'
        self.assertEqual(marks(text), [("start", "the build")])
        self.assertEqual(outcome_block(text), '{"status": "done"}')

    def test_a_mark_inside_a_fence_is_none(self):
        for text in (
            "```\n[agent-pm-progress:start] x\n```",
            "~~~md\n[agent-pm-progress:start] x\n~~~",
            "```agent-pm-outcome\n[agent-pm-progress:start] x\n```",
            "````\n```\n[agent-pm-progress:a] x",
        ):
            with self.subTest(text=text):
                self.assertEqual(marks(text), [])

    def test_a_bullet_or_leading_whitespace_is_accepted(self):
        text = "- [agent-pm-progress:a] one\n* [agent-pm-progress:b] two\n   [agent-pm-progress:c] three"
        self.assertEqual(marks(text), [("a", "one"), ("b", "two"), ("c", "three")])

    def test_a_mark_not_at_the_line_start_is_none(self):
        for text in (
            "see [agent-pm-progress:start] x",
            "+ [agent-pm-progress:start] x",
            "-[agent-pm-progress:start] x",
            "`[agent-pm-progress:start] x`",
        ):
            with self.subTest(text=text):
                self.assertEqual(marks(text), [])

    def test_a_bad_name_or_empty_text_is_skipped(self):
        for text in (
            "[agent-pm-progress:a b] x",
            "[agent-pm-progress:] x",
            "[agent-pm-progress:a.b] x",
            "[agent-pm-progress:start]",
            "[agent-pm-progress:start]  \t ",
        ):
            with self.subTest(text=text):
                self.assertEqual(marks(text), [])
        self.assertEqual(marks("[agent-pm-progress:plan-ready_2] ok"), [("plan-ready_2", "ok")])

    def test_whitespace_runs_in_a_line_become_one_space(self):
        self.assertEqual(
            marks("[agent-pm-progress:start]   first build \t of  the PRD  \r"),
            [("start", "first build of the PRD")],
        )
        self.assertEqual(
            marks("[agent-pm-progress:start] first build\n  of the PRD"), [("start", "first build")]
        )

    def test_several_marks_in_order(self):
        text = "[agent-pm-progress:start] a\nx\n[agent-pm-progress:step] b\n[agent-pm-progress:start] c"
        self.assertEqual(marks(text), [("start", "a"), ("step", "b"), ("start", "c")])

    def test_no_text_is_no_mark(self):
        self.assertEqual(marks(""), [])

    def test_the_mark_name_is_composers(self):
        self.assertTrue(MARK.match(f"[{PROGRESS}:start] x"))


class OutcomeBlock(Case):
    def test_the_last_of_several_blocks(self):
        text = '```agent-pm-outcome\n{"a": 1}\n```\nthen\n~~~agent-pm-outcome\n{"b": 2}\n~~~\n'
        self.assertEqual(outcome_block(text), '{"b": 2}')

    def test_none(self):
        for text in ("", "no block", "```\n{}\n```", "agent-pm-outcome {}"):
            with self.subTest(text=text):
                self.assertIsNone(outcome_block(text))

    def test_an_unclosed_block_runs_to_the_end(self):
        self.assertEqual(outcome_block('x\n```agent-pm-outcome\n{"a": 1}\n'), '{"a": 1}\n')

    def test_an_empty_block_is_empty_text(self):
        self.assertEqual(outcome_block("```agent-pm-outcome\n```"), "")

    def test_the_info_string_is_stripped(self):
        self.assertEqual(outcome_block("```  agent-pm-outcome \t\n{}\n```"), "{}")

    def test_a_block_nested_in_a_longer_fence_is_none(self):
        self.assertIsNone(outcome_block("````md\n```agent-pm-outcome\n{}\n```\n````\n"))

    def test_a_block_with_another_info_string_is_ignored(self):
        text = (
            '```agent-pm-outcome\n{"a": 1}\n```\n```json\n{}\n```\n```agent-pm-outcome x\n{}\n```\n'
        )
        self.assertEqual(outcome_block(text), '{"a": 1}')
        self.assertIsNone(outcome_block("```agent-pm-outcomes\n{}\n```"))


if __name__ == "__main__":
    unittest.main()
