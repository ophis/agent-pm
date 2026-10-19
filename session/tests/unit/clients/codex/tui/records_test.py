"""Codex rollout message records: role and joined text."""

from agent_pm.session.clients.codex.tui import records

from session.tests.case import Case


def _said(role, *blocks):
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": list(blocks)},
    }


class MessageTest(Case):
    def test_user_input_text_joined(self):
        record = _said(
            "user",
            {"type": "input_text", "text": "a"},
            {"type": "input_image", "image_url": "x"},
            {"type": "input_text", "text": "b"},
        )
        self.assertEqual(records.message(record), ("user", "a\nb"))

    def test_assistant_output_text_joined(self):
        record = _said(
            "assistant",
            {"type": "output_text", "text": "one"},
            {"type": "input_text", "text": "not mine"},
            {"type": "output_text", "text": "two"},
        )
        self.assertEqual(records.message(record), ("assistant", "one\ntwo"))

    def test_empty_text_is_still_a_message(self):
        self.assertEqual(records.message(_said("assistant")), ("assistant", ""))

    def test_other_records_are_none(self):
        for record in (
            {"type": "response_item", "payload": {"type": "function_call", "name": "x"}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer"}},
            {"type": "response_item", "payload": None},
            {"type": "event_msg", "payload": {"type": "message", "role": "user"}},
        ):
            with self.subTest(record=record):
                self.assertIsNone(records.message(record))

    def test_content_that_is_not_text_blocks_is_empty(self):
        content = {"type": "response_item", "payload": {"type": "message", "role": "user"}}
        content["payload"]["content"] = 5
        self.assertEqual(records.message(content), ("user", ""))
        record = _said("assistant", "text", {"type": "output_text", "text": 5}, None)
        self.assertEqual(records.message(record), ("assistant", ""))
