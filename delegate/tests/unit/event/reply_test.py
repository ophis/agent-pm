"""delegate/event/reply.py: progress mark lines and the outcome block of a reply (FR-18)."""

import unittest

from agent_pm.delegate.event.reply import marks, outcome_block

from delegate.tests.case import Case


class Marks(Case):
    def test_mark_lines_give_their_name_and_text_in_order(self):
        for text, want in (
            (
                "Reading the task.\n[agent-pm-progress:start] first build of the PRD\nNow the spec.",
                [("start", "first build of the PRD")],
            ),
            (
                "- [agent-pm-progress:a] one\n* [agent-pm-progress:b] two\n   [agent-pm-progress:c] three",
                [("a", "one"), ("b", "two"), ("c", "three")],
            ),
            (
                "[agent-pm-progress:start]   first build \t of  the PRD  \r",
                [("start", "first build of the PRD")],
            ),
            ("[agent-pm-progress:start] first build\n  of the PRD", [("start", "first build")]),
            (
                "[agent-pm-progress:start] a\nx\n[agent-pm-progress:step] b\n[agent-pm-progress:start] c",
                [("start", "a"), ("step", "b"), ("start", "c")],
            ),
        ):
            with self.subTest(text=text):
                self.assertEqual(marks(text), want)

    def test_a_mark_and_an_outcome_block_are_one_mark_and_the_block(self):
        text = '[agent-pm-progress:start] the build\nDone.\n```agent-pm-outcome\n{"status": "done"}\n```\n'
        self.assertEqual(marks(text), [("start", "the build")])
        self.assertEqual(outcome_block(text), '{"status": "done"}')

    def test_a_mark_in_a_fence_or_not_at_the_line_start_or_no_text_is_none(self):
        for text in (
            "```\n[agent-pm-progress:start] x\n```",
            "~~~md\n[agent-pm-progress:start] x\n~~~",
            "```agent-pm-outcome\n[agent-pm-progress:start] x\n```",
            "````\n```\n[agent-pm-progress:a] x",
            "see [agent-pm-progress:start] x",
            "+ [agent-pm-progress:start] x",
            "-[agent-pm-progress:start] x",
            "`[agent-pm-progress:start] x`",
            "",
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


class OutcomeBlock(Case):
    def test_the_last_blocks_text(self):
        for text, want in (
            (
                '```agent-pm-outcome\n{"a": 1}\n```\nthen\n~~~agent-pm-outcome\n{"b": 2}\n~~~\n',
                '{"b": 2}',
            ),
            ('x\n```agent-pm-outcome\n{"a": 1}\n', '{"a": 1}\n'),
            ("```agent-pm-outcome\n```", ""),
            ("```  agent-pm-outcome \t\n{}\n```", "{}"),
        ):
            with self.subTest(text=text):
                self.assertEqual(outcome_block(text), want)

    def test_no_block_or_one_nested_in_a_longer_fence_is_none(self):
        for text in (
            "",
            "no block",
            "```\n{}\n```",
            "agent-pm-outcome {}",
            "````md\n```agent-pm-outcome\n{}\n```\n````\n",
        ):
            with self.subTest(text=text):
                self.assertIsNone(outcome_block(text))

    def test_a_block_with_another_info_string_is_ignored(self):
        text = (
            '```agent-pm-outcome\n{"a": 1}\n```\n```json\n{}\n```\n```agent-pm-outcome x\n{}\n```\n'
        )
        self.assertEqual(outcome_block(text), '{"a": 1}')
        self.assertIsNone(outcome_block("```agent-pm-outcomes\n{}\n```"))


if __name__ == "__main__":
    unittest.main()
