"""utils/record.py: Record and Status lines, round trips and refusals."""

import json
import unittest
from dataclasses import asdict

from agent_pm.utils.record import Record, RecordError, Status

from tests.case import Case

FULL = Record(
    v=1,
    kind="delegate",
    name="engineer-1234abcd",
    sessions=("engineer-1234abcd-drive", "engineer-1234abcd-gate"),
    sid="1234abcd-0000-4000-8000-000000000000",
    workdir="/w/run 1",
    ctl=("agent-pm-delegate",),
    resume=("agent-pm-delegate", "resume", "/w/run 1"),
    display="elsewhere:1.2",
    detail="engineer: 修复",
)
MINIMAL = Record(
    1, "fake", "n", (), None, None, ("fake-ctl",), ("fake-ctl", "resume", "n"), None, None
)
MINIMAL_LINE = (
    '{"v":1,"kind":"fake","name":"n","sessions":[],"sid":null,"workdir":null,'
    '"ctl":["fake-ctl"],"resume":["fake-ctl","resume","n"],"display":null,"detail":null}'
)
CROSS = "/x/bin/agent-pm-crew"
TEXT = "want a non-empty printable str"
NULL_TEXT = "want null or a non-empty printable str"
ARGV = "want a non-empty list of str"
SESSIONS = "sessions: want a list of non-empty printable str"
STATUS = Status("n", True, "working", "done", "fake-ctl attach n")


def line(base, **changes) -> str:
    return json.dumps({**asdict(base), **changes})


class RecordLine(Case):
    def refused(self, text, message, cls=Record):
        with self.assertRaises(RecordError) as caught:
            cls.parse(text)
        self.assertEqual(str(caught.exception), message)

    def test_full_and_minimal_round_trip(self):
        for record in (FULL, MINIMAL):
            with self.subTest(record=record):
                text = record.to_line()
                self.assertEqual(Record.parse(text), record)
                self.assertEqual(Record.parse(text + "\n"), record)
                self.assertEqual(text.count("\n"), 0)
                self.assertIsInstance(Record.parse(text).sessions, tuple)
        self.assertEqual(MINIMAL.to_line(), MINIMAL_LINE)
        self.assertEqual(list(json.loads(FULL.to_line())), list(asdict(FULL)))

    def test_to_line_is_one_line_whatever_the_display_holds(self):
        record = Record(**{**asdict(MINIMAL), "display": "a\nb c\r"})
        text = record.to_line()
        self.assertEqual(text.splitlines(), [text])
        self.assertEqual(Record.parse(text), record)

    def test_detail_with_a_newline_is_refused(self):
        self.refused(line(FULL, detail="a\nb"), f"detail: {NULL_TEXT}")
        with self.assertRaises(RecordError):
            Record(**{**asdict(FULL), "detail": "a\nb"})

    def test_v_must_be_the_int_one(self):
        for v in (2, True, "1", 1.0, None):
            with self.subTest(v=v):
                self.refused(line(FULL, v=v), "v: want 1")

    def test_each_missing_field_is_named(self):
        for field in asdict(FULL):
            with self.subTest(field=field):
                data = asdict(FULL)
                del data[field]
                self.refused(json.dumps(data), f"{field}: missing")

    def test_an_extra_key_is_named(self):
        self.refused(line(FULL, extra=1), "extra: unknown field")

    def test_ctl_and_resume_start_with_a_command_name(self):
        for field in ("ctl", "resume"):
            for value in ([CROSS], [CROSS, "resume", "x"], ["", "x"], ["bin/x"]):
                with self.subTest(field=field, value=value):
                    self.refused(
                        line(FULL, **{field: value}), f"{field}[0] must be a command name without /"
                    )
            for value in ([], "fake-ctl", ["fake-ctl", 1], None):
                with self.subTest(field=field, value=value):
                    self.refused(line(FULL, **{field: value}), f"{field}: {ARGV}")

    def test_each_text_field_rule(self):
        for field, values, want in (
            ("kind", ("", "a\tb", None, 1), TEXT),
            ("name", ("", "a\nb", ["n"]), TEXT),
            ("sid", ("", "a\x1bb", 1, []), NULL_TEXT),
            ("workdir", ("", "/w\n"), NULL_TEXT),
            ("detail", ("", "\x07", 1), NULL_TEXT),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.refused(line(FULL, **{field: value}), f"{field}: {want}")

    def test_sessions_is_a_list_of_names(self):
        for value in ("a", None, ["a", ""], ["a\n"], [1], [["a"]]):
            with self.subTest(value=value):
                self.refused(line(FULL, sessions=value), SESSIONS)

    def test_display_is_null_or_a_str(self):
        self.assertEqual(Record.parse(line(FULL, display="")).display, "")
        for value in (1, ["a"], {"a": 1}):
            with self.subTest(value=value):
                self.refused(line(FULL, display=value), "display: want null or a str")

    def test_not_a_json_object(self):
        for text in ("", "{", "not json", "[]", '["v", 1]', "1", "null"):
            with self.subTest(text=text), self.assertRaises(RecordError):
                Record.parse(text)
        self.refused("[1]", "Record line: want a JSON object")
        with self.assertRaises(RecordError) as caught:
            Record.parse("{")
        self.assertTrue(str(caught.exception).startswith("Record line: not JSON"), caught.exception)

    def test_the_constructor_validates_too(self):
        with self.assertRaises(RecordError) as caught:
            Record(**{**asdict(MINIMAL), "ctl": ["fake-ctl"]})
        self.assertEqual(str(caught.exception), f"ctl: {ARGV}")
        self.assertIsInstance(caught.exception, ValueError)


class StatusLine(Case):
    def refused(self, text, message):
        with self.assertRaises(RecordError) as caught:
            Status.parse(text)
        self.assertEqual(str(caught.exception), message)

    def test_round_trip(self):
        for status in (STATUS, Status("n", False, "gone", None, None)):
            with self.subTest(status=status):
                text = status.to_line()
                self.assertNotIn("\n", text)
                self.assertEqual(Status.parse(text + "\n"), status)
        self.assertEqual(
            Status("n", False, "gone", None, None).to_line(),
            '{"name":"n","alive":false,"state":"gone","outcome":null,"attach":null}',
        )

    def test_each_missing_key_and_an_extra_one(self):
        for field in asdict(STATUS):
            with self.subTest(field=field):
                data = asdict(STATUS)
                del data[field]
                self.refused(json.dumps(data), f"{field}: missing")
        self.refused(line(STATUS, v=1), "v: unknown field")

    def test_each_field_rule(self):
        for field, value, message in (
            ("alive", "yes", "alive: want true or false"),
            ("alive", 1, "alive: want true or false"),
            ("name", "", f"name: {TEXT}"),
            ("state", "a\nb", f"state: {TEXT}"),
            ("state", None, f"state: {TEXT}"),
            ("outcome", 1, "outcome: want null or a str"),
            ("attach", ["fake-ctl"], "attach: want null or a str"),
        ):
            with self.subTest(field=field, value=value):
                self.refused(line(STATUS, **{field: value}), message)

    def test_not_a_json_object(self):
        self.refused("[]", "Status line: want a JSON object")


class DeepLine(Case):
    def test_a_line_too_deep_to_decode_is_not_json(self):
        for cls in (Record, Status):
            for text in ("[" * 200000, '{"a":' * 200000):
                with self.subTest(cls=cls, text=text[:5]):
                    with self.assertRaises(RecordError) as caught:
                        cls.parse(text)
                    message = str(caught.exception)
                    self.assertTrue(message.startswith(f"{cls.__name__} line: not JSON"), message)


if __name__ == "__main__":
    unittest.main()
