"""utils/record.py: Record and Status lines, round trips and refusals; answer_keys."""

import json
import unittest
from dataclasses import asdict, replace
from io import StringIO

from agent_pm.utils import answer_keys, number
from agent_pm.utils.record import Record, RecordError, Status

from utils.tests.case import Case

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
    1,
    "fake",
    "n",
    (),
    None,
    None,
    ("agent-pm-session",),
    ("agent-pm-session", "resume", "n"),
    None,
    None,
)
MINIMAL_LINE = (
    '{"v":1,"kind":"fake","name":"n","sessions":[],"sid":null,"workdir":null,'
    '"ctl":["agent-pm-session"],"resume":["agent-pm-session","resume","n"],"display":null,'
    '"detail":null}'
)
CTL = "ctl: want [C], C one of agent-pm-delegate, agent-pm-linear, agent-pm-session"
RESUME = "resume: want [ctl[0], resume, KEY], KEY printable and not starting with -"
CROSS = "/x/bin/agent-pm-crew"
TEXT = "want a non-empty printable str"
NULL_TEXT = "want null or a non-empty printable str"
ARGV = "want a non-empty list of str"
SESSIONS = "sessions: want a list of non-empty printable str"
STATUS = Status(
    name="n", alive=True, state="working", outcome="done", attach="tmux attach -t n", cwd="/w/repo"
)


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

    def test_each_missing_field_is_named(self):
        for field in asdict(FULL):
            with self.subTest(field=field):
                data = asdict(FULL)
                del data[field]
                self.refused(json.dumps(data), f"{field}: missing")

    def test_each_field_rule_names_the_field(self):
        for field, values, want in (
            ("v", (2, True, "1", 1.0, None), "v: want 1"),
            ("extra", (1,), "extra: unknown field"),
            *(
                (field, ([], "agent-pm-session", ["agent-pm-session", 1], None), f"{field}: {ARGV}")
                for field in ("ctl", "resume")
            ),
            (
                "ctl",
                (
                    [CROSS],
                    ["sh", "-c", "touch /tmp/pwned"],
                    ["agent-pm-crew"],
                    ["fake-ctl"],
                    ["/usr/bin/agent-pm-session"],
                    ["agent-pm-session", "status"],
                    ["agent-pm-delegate", "--config", "/x"],
                    [""],
                ),
                CTL,
            ),
            (
                "resume",
                (
                    ["sh", "-c", "touch /tmp/pwned"],
                    ["agent-pm-session", "resume", "/w/run 1"],
                    ["agent-pm-delegate", "resume"],
                    ["agent-pm-delegate", "resume", "/w", "--x"],
                    ["agent-pm-delegate", "start", "/w"],
                    ["agent-pm-delegate", "resume", "--help"],
                    ["agent-pm-delegate", "resume", "-"],
                    ["agent-pm-delegate", "resume", ""],
                    ["agent-pm-delegate", "resume", "a\nb"],
                    [CROSS, "resume", "x"],
                ),
                RESUME,
            ),
            ("kind", ("", "a\tb", None, 1), f"kind: {TEXT}"),
            ("name", ("", "a\nb", ["n"]), f"name: {TEXT}"),
            ("sid", ("", "a\x1bb", 1, []), f"sid: {NULL_TEXT}"),
            ("workdir", ("", "/w\n"), f"workdir: {NULL_TEXT}"),
            ("detail", ("", "\x07", 1), f"detail: {NULL_TEXT}"),
            ("sessions", ("a", None, ["a", ""], ["a\n"], [1], [["a"]]), SESSIONS),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.refused(line(FULL, **{field: value}), want)

    def test_each_layer_commands_exact_forms_are_accepted(self):
        for command in ("agent-pm-delegate", "agent-pm-linear", "agent-pm-session"):
            with self.subTest(command=command):
                record = Record.parse(
                    line(FULL, ctl=[command], resume=[command, "resume", "TASK-1 /w"])
                )
                self.assertEqual(record.ctl, (command,))

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
            Record(**{**asdict(MINIMAL), "ctl": ["agent-pm-session"]})
        self.assertEqual(str(caught.exception), f"ctl: {ARGV}")
        self.assertIsInstance(caught.exception, ValueError)


class NumberTest(Case):
    def test_finite_numeric_values_become_floats(self):
        for value, expected in ((0, 0.0), (-2, -2.0), (1.5, 1.5), (10**100, 1e100)):
            with self.subTest(value=value):
                converted = number(value)
                self.assertEqual(converted, expected)
                self.assertIsInstance(converted, float)

    def test_non_numeric_and_non_finite_values_are_absent(self):
        for value in (True, False, None, "1", [], {}, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(number(value))
        self.assertIsNone(number(10**1000))


class StatusLine(Case):
    def refused(self, text, message):
        with self.assertRaises(RecordError) as caught:
            Status.parse(text)
        self.assertEqual(str(caught.exception), message)

    def test_round_trip(self):
        for status in (
            STATUS,
            Status(name="n", alive=False, state="gone", outcome=None, attach=None),
        ):
            with self.subTest(status=status):
                text = status.to_line()
                self.assertNotIn("\n", text)
                self.assertEqual(Status.parse(text + "\n"), status)
        self.assertEqual(
            Status(name="n", alive=False, state="gone", outcome=None, attach=None).to_line(),
            '{"name":"n","alive":false,"state":"gone","outcome":null,"attach":null,"cwd":null}',
        )

    def test_a_line_without_cwd_has_none(self):
        data = asdict(STATUS)
        del data["cwd"]
        self.assertEqual(Status.parse(json.dumps(data)), replace(STATUS, cwd=None))

    def test_each_missing_key_and_an_extra_one(self):
        for field in [field for field in asdict(STATUS) if field != "cwd"]:
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
            ("cwd", "", f"cwd: {NULL_TEXT}"),
            ("cwd", "/w\n", f"cwd: {NULL_TEXT}"),
            ("cwd", 1, f"cwd: {NULL_TEXT}"),
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


class Usage(Exception):
    pass


class Failure(Exception):
    pass


RESOLVE_ERRORS = {"u": (Usage, "not a key"), "f": (Failure, "not found")}
REPLY_ERRORS = {"ru": (Usage, "cannot answer"), "rf": (Failure, "lost")}
NOT_JSON = "Record line: not JSON (Expecting value: line 1 column 1 (char 0))"


def record_line(name: str) -> str:
    resume = ("agent-pm-delegate", "resume", "/w")
    record = Record(1, "role", name, (), None, "/w", ("agent-pm-delegate",), resume, None, None)
    return record.to_line()


def label(key: str | Record) -> str:
    return key if isinstance(key, str) else key.name


def reply_to(value: str) -> str | None:
    if value in REPLY_ERRORS:
        kind, message = REPLY_ERRORS[value]
        raise kind(message)
    return None if value == "q" else f"{value}: ok"


class AnswerKeys(Case):
    def setUp(self):
        self.resolved = []

    def resolve(self, key: str | Record) -> tuple[str, str]:
        self.resolved.append(key)
        if label(key) in RESOLVE_ERRORS:
            kind, message = RESOLVE_ERRORS[label(key)]
            raise kind(message)
        return label(key), label(key)

    def answer(
        self, args=(), stdin: str | StringIO = "", *, resolve=None, reply=reply_to, single=False
    ):
        out, err = StringIO(), StringIO()
        code = answer_keys(
            args,
            StringIO(stdin) if isinstance(stdin, str) else stdin,
            resolve or self.resolve,
            reply,
            single=single,
            usage=(Usage,),
            failures=(Failure,),
            prog="prog",
            out=out,
            err=err,
        )
        return code, out.getvalue(), err.getvalue()

    def test_args_win_over_stdin(self):
        stdin = StringIO("not json\n")
        self.assertEqual(self.answer(["a", "b"], stdin), (0, "a: ok\nb: ok\n", ""))
        self.assertEqual(stdin.read(), "not json\n")

    def test_stdin_records_are_resolved_in_order(self):
        stdin = f"{record_line('b')}\n{record_line('f')}\n{record_line('a')}"
        self.assertEqual(self.answer((), stdin), (1, "b: ok\na: ok\n", "prog: f: not found\n"))
        self.assertEqual(self.resolved, [Record.parse(record_line(n)) for n in ("b", "f", "a")])
        self.assertEqual(self.answer((), ""), (0, "", ""))

    def test_a_bad_stdin_line_stops_before_any_key(self):
        for lines, error in (
            ((record_line("a"), "not json"), f"stdin line 2: {NOT_JSON}"),
            (('{"v": 1}', record_line("a")), "stdin line 1: kind: missing"),
            ((record_line("a"), "[1]"), "stdin line 2: Record line: want a JSON object"),
            (
                (record_line("a"), record_line("b"), "", record_line("c")),
                f"stdin line 3: {NOT_JSON}",
            ),
            ((record_line("a"), "\n"), f"stdin line 2: {NOT_JSON}"),
        ):
            with self.subTest(lines=lines):
                stdin = "\n".join(lines) + "\n"
                self.assertEqual(self.answer((), stdin), (2, "", f"prog: {error}\n"))
                self.assertEqual(self.resolved, [])

    def test_a_failing_key_is_named_later_keys_are_still_answered_and_none_writes_no_line(self):
        for args, code, err in (
            (("a", "q", "b"), 0, ""),
            (("a", "u", "b"), 2, "prog: u: not a key\n"),
            (("a", "f", "b"), 1, "prog: f: not found\n"),
            (("f", "a", "u", "b"), 2, "prog: f: not found\nprog: u: not a key\n"),
            (("a", "ru", "b"), 2, "prog: ru: cannot answer\n"),
            (("a", "rf", "b"), 1, "prog: rf: lost\n"),
            (("rf", "a", "ru", "b"), 2, "prog: rf: lost\nprog: ru: cannot answer\n"),
            (("a", "u", "rf", "b"), 2, "prog: u: not a key\nprog: rf: lost\n"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.answer(args), (code, "a: ok\nb: ok\n", err))

    def test_a_name_given_twice_answers_nothing(self):
        self.assertEqual(
            self.answer(("a", "b", "a")), (2, "", "prog: name a given more than once\n")
        )
        self.assertEqual(self.resolved, ["a", "b", "a"])
        stdin = f"{record_line('b')}\n{record_line('b')}\n"
        self.assertEqual(self.answer((), stdin), (2, "", "prog: name b given more than once\n"))

    def test_single_takes_exactly_one_key(self):
        for args, stdin, n in (
            ((), "", 0),
            (("a", "b"), "", 2),
            ((), f"{record_line('a')}\n" * 2, 2),
        ):
            with self.subTest(args=args, stdin=stdin):
                self.assertEqual(
                    self.answer(args, stdin, single=True),
                    (2, "", f"prog: takes exactly one key, got {n}\n"),
                )
        self.assertEqual(self.resolved, [])
        self.assertEqual(self.answer(("a",), single=True), (0, "a: ok\n", ""))
        self.assertEqual(self.answer((), record_line("b"), single=True), (0, "b: ok\n", ""))

    def test_errors_reach_stderr_without_control_characters(self):
        def resolve(_key):
            raise Failure("\x1b]52;c;x\x07 gone\n")

        self.assertEqual(
            self.answer(("\x1b[2Ja",), resolve=resolve), (1, "", "prog: [2Ja: ]52;c;x gone\n")
        )

    def test_an_unclassified_exception_propagates(self):
        def reply(_value):
            raise RuntimeError("bug")

        with self.assertRaises(KeyError):
            self.answer(("a",), resolve=lambda key: {}[key])
        with self.assertRaises(RuntimeError):
            self.answer(("a",), reply=reply)


if __name__ == "__main__":
    unittest.main()
